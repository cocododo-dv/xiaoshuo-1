#!/usr/bin/env bash
# Cross-platform React contract E2E lane used by Linux developers and CI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
REACT_DIR="$REPO_ROOT/frontend-react"
RUN_DIR="$REPO_ROOT/.codex-run/e2e-linux"

# Never the dev stack's ports (backend 8000, React 5174): an author's running stack there would
# answer this lane's readiness probes and the smokes would drive the live app and its database.
BACKEND_PORT="${PLAYWRIGHT_BACKEND_PORT:-8009}"
REACT_PORT="${PLAYWRIGHT_REACT_PORT:-5176}"
READY_TIMEOUT_SECONDS="${PLAYWRIGHT_READY_TIMEOUT_SECONDS:-120}"
PYTHON_BIN="${NOVEL_SYSTEM_PYTHON:-python}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
REACT_URL="http://127.0.0.1:${REACT_PORT}/"

# Refuse (exit 2) before touching anything when a chosen port is unusable or already served:
# this lane only stops the processes it starts, so with another server on the port the readiness
# probe below would pass against that server and the smokes would run against it.
refuse_to_start() {
  echo "verify_react_e2e: refusing to start: $1" >&2
  exit 2
}

port_is_listening() {
  timeout 3 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" 2>/dev/null
}

check_port() {
  local label="$1" port="$2" variable="$3"
  if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    refuse_to_start "$variable=$port is not a TCP port."
  fi
  if port_is_listening "$port"; then
    refuse_to_start "the $label port $port already has a listener on 127.0.0.1. Stop it or pick a free port with $variable=<port>."
  fi
}

check_port "backend" "$BACKEND_PORT" PLAYWRIGHT_BACKEND_PORT
check_port "React" "$REACT_PORT" PLAYWRIGHT_REACT_PORT
if [ "$BACKEND_PORT" = "$REACT_PORT" ]; then
  refuse_to_start "the backend and React ports are both $BACKEND_PORT."
fi

mkdir -p "$RUN_DIR"
DB_PATH="$RUN_DIR/e2e.db"
BACKEND_LOG="$RUN_DIR/backend.log"
REACT_LOG="$RUN_DIR/react.log"
rm -f "$DB_PATH" "$DB_PATH-shm" "$DB_PATH-wal"

BACKEND_PID=""
REACT_PID=""

stop_process_group() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 0.25
    done
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  stop_process_group "$REACT_PID"
  stop_process_group "$BACKEND_PID"
}
trap cleanup EXIT INT TERM

# Wait until our own process answers at the URL: if it exits first (e.g. the port was taken after
# the preflight), fail instead of trusting whatever else answers there.
wait_http() {
  local label="$1" url="$2" pid="$3" waited=0
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "The ${label} process exited before answering at ${url}" >&2
      return 1
    fi
    if [ "$waited" -ge "$READY_TIMEOUT_SECONDS" ]; then
      echo "Timed out waiting for ${label}: ${url}" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Something answered at ${url} but the ${label} process is gone; refusing to run the smokes against it" >&2
    return 1
  fi
}

if [ ! -f "$REACT_DIR/node_modules/playwright/package.json" ]; then
  echo "Playwright is missing. Run: cd frontend-react && npm ci" >&2
  exit 1
fi

export PYTHONPATH="$BACKEND_DIR/src"
export NOVEL_SYSTEM_PYTHON="$PYTHON_BIN"
export NOVEL_SYSTEM_DATABASE_URL="sqlite:///${DB_PATH//\\//}"
export NOVEL_SYSTEM_CONFIG_SECRET="e2e-${RANDOM}-$$"
export NOVEL_SYSTEM_LLM_ENABLED="false"
export NOVEL_SYSTEM_CORS_ORIGINS="${REACT_URL%/}"

echo "==> Migrating isolated E2E database"
(
  cd "$BACKEND_DIR"
  "$PYTHON_BIN" -m alembic upgrade head
)

echo "==> Starting backend at $BACKEND_URL"
(
  cd "$BACKEND_DIR"
  exec setsid "$PYTHON_BIN" -m uvicorn novel_system.api.app:create_app \
    --factory --host 127.0.0.1 --port "$BACKEND_PORT" --app-dir src
) >"$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

echo "==> Starting React at $REACT_URL"
(
  cd "$REACT_DIR"
  export VITE_NOVEL_SYSTEM_API_BASE="$BACKEND_URL"
  exec setsid npm run dev -- --host 127.0.0.1 --port "$REACT_PORT" --strictPort
) >"$REACT_LOG" 2>&1 &
REACT_PID=$!

if ! wait_http "backend" "$BACKEND_URL/ready" "$BACKEND_PID"; then
  tail -n 120 "$BACKEND_LOG" >&2 || true
  exit 1
fi
if ! wait_http "React dev server" "$REACT_URL" "$REACT_PID"; then
  tail -n 120 "$REACT_LOG" >&2 || true
  exit 1
fi

echo "==> Running React contract E2E suites"
set +e
(
  cd "$REACT_DIR"
  node scripts/run-smokes.mjs "$REACT_URL" "$BACKEND_URL"
)
E2E_STATUS=$?
set -e

if [ "$E2E_STATUS" -ne 0 ]; then
  echo "React contract E2E failed. Backend log:" >&2
  tail -n 160 "$BACKEND_LOG" >&2 || true
  echo "React log:" >&2
  tail -n 160 "$REACT_LOG" >&2 || true
  exit "$E2E_STATUS"
fi

echo "React contract E2E passed."

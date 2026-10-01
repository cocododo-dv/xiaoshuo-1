#!/usr/bin/env bash
# Shared helpers for scripts/start-*-linux.sh / start-all-linux.sh /
# stop-all-linux.sh. Meant to be sourced, not executed directly.

# Terminate the process group recorded in pidfile $1 (if still alive), then
# remove the pidfile. Leg scripts write their own $$ to their pidfile right
# before the final `exec`, and are always run as their own process-group
# leader (either via interactive job control, or via start-all-linux.sh's
# `setsid`), so `-$pid` reliably reaches the whole subtree — e.g. uvicorn
# --reload's worker, or npm's vite child.
dev_stop_pidfile() {
  local pid_file="$1" pid
  [ -f "$pid_file" ] || return 0
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  rm -f "$pid_file"
  [ -n "${pid:-}" ] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

# Fallback cleanup for whatever is bound to TCP port $1 when there's no (or a
# stale) pidfile — e.g. a previous run that was killed out-of-band.
dev_stop_port() {
  local port="$1" pid
  while pid="$(ss -H -ltnp "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)" && [ -n "$pid" ]; do
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.5
    kill -KILL "$pid" 2>/dev/null || true
  done
}

# Poll a URL until it answers 2xx/3xx (return 0), or time out (return 1).
# With an optional third argument — the pid of the leg being waited on — give
# up early with return 2 as soon as that process is gone, so a leg that dies
# during startup (missing toolchain, failed migration, ...) fails fast instead
# of silently eating the whole timeout. Legs `exec` their server as the final
# step, so the pid stays valid for the lifetime of the server.
dev_wait_http_ok() {
  local url="$1" timeout="${2:-90}" pid="${3:-}" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    curl -fsS -o /dev/null "$url" 2>/dev/null && return 0
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      return 2
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

# Oldest Node the React frontend runs on — keep equal to frontend-react/package.json
# "engines" (ESLint 9's floor; Vite 6 / Vitest 3 need 18 as well). Node 22 is the
# version CI tests and the one every message recommends.
DEV_NODE_FLOOR_MAJOR=18
DEV_NODE_FLOOR_MINOR=18

# Put a Node toolchain for the React frontend on PATH — first hit wins:
#   1. $NOVEL_SYSTEM_NODE_BIN   directory holding node/npm (explicit override)
#   2. nvm ($NVM_DIR, ~/.nvm)   sourced; its default alias puts node on PATH
#   3. ~/.local/node/bin        a plain tarball install (the Ubuntu dev host)
#   4. whatever `node` is already on PATH
dev_use_frontend_node() {
  if [ -n "${NOVEL_SYSTEM_NODE_BIN:-}" ]; then
    export PATH="$NOVEL_SYSTEM_NODE_BIN:$PATH"
  elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"
  elif [ -x "$HOME/.local/node/bin/node" ]; then
    export PATH="$HOME/.local/node/bin:$PATH"
  fi
}

dev_node_install_hint() {
  cat >&2 <<'MSG'
   Install Node 22 (the version CI tests) and point NOVEL_SYSTEM_NODE_BIN at its bin/
   directory, unpack it at ~/.local/node, or make it nvm's default (nvm alias default 22).
   CentOS 7 / glibc 2.17: use Node 22's glibc-217 build from
   https://unofficial-builds.nodejs.org/download/release/
MSG
}

# Check — without stopping or starting anything — that the React frontend in
# directory $1 can run: node + npm on PATH (call dev_use_frontend_node first),
# Node >= DEV_NODE_FLOOR_*, node_modules installed. Prints what to do and returns
# 1 otherwise. Call it BEFORE dev_stop_pidfile / dev_stop_port: a relaunch with a
# missing or too-old Node must leave the running frontend alone.
dev_check_frontend_toolchain() {
  local frontend_dir="$1" node_path version major=0 minor=0
  if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    echo "!! node/npm not found: the React frontend needs Node >= ${DEV_NODE_FLOOR_MAJOR}.${DEV_NODE_FLOOR_MINOR}." >&2
    dev_node_install_hint
    return 1
  fi
  node_path="$(command -v node)"
  version="$(node --version 2>&1)" || true
  version="${version%%$'\n'*}"
  if [[ "$version" =~ ^v([0-9]+)\.([0-9]+)\. ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
  fi
  if [ "$major" -lt "$DEV_NODE_FLOOR_MAJOR" ] \
    || { [ "$major" -eq "$DEV_NODE_FLOOR_MAJOR" ] && [ "$minor" -lt "$DEV_NODE_FLOOR_MINOR" ]; }; then
    echo "!! \`${node_path} --version\` says '${version:-nothing}': the React frontend needs Node >= ${DEV_NODE_FLOOR_MAJOR}.${DEV_NODE_FLOOR_MINOR}." >&2
    dev_node_install_hint
    return 1
  fi
  if [ ! -d "$frontend_dir/node_modules" ]; then
    echo "!! frontend-react/node_modules is missing. Run: cd frontend-react && npm ci" >&2
    return 1
  fi
}

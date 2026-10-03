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
# "engines" (ESLint 10's floor, approved 2026-10-03; Vite 6 / Vitest 3 only need 18).
# ESLint 10 itself takes ^20.19 || ^22.13 || >=24: on Node 21 or 22.0–22.12 the app runs
# and only `npm run lint` refuses. Node 22 is the version CI tests and the one every
# message recommends.
DEV_NODE_FLOOR_MAJOR=20
DEV_NODE_FLOOR_MINOR=19

# Put a Node toolchain for the React frontend on PATH — first hit wins:
#   1. $NOVEL_SYSTEM_NODE_BIN   directory holding node/npm (explicit override)
#   2. nvm ($NVM_DIR, ~/.nvm)   sourced; a hit only when that leaves one of nvm's own
#                               nodes on PATH (its default alias, or a version that
#                               is already active) — without one, go on to 3 / 4
#   3. ~/.local/node/bin        a plain tarball install (the Ubuntu dev host)
#   4. whatever `node` is already on PATH
dev_use_frontend_node() {
  if [ -n "${NOVEL_SYSTEM_NODE_BIN:-}" ]; then
    export PATH="$NOVEL_SYSTEM_NODE_BIN:$PATH"
    return 0
  fi
  if [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"
    case "$(command -v node 2>/dev/null)" in
      "${NVM_DIR%/}"/*) return 0 ;;
    esac
  fi
  if [ -x "$HOME/.local/node/bin/node" ]; then
    export PATH="$HOME/.local/node/bin:$PATH"
  fi
}

# What to do about a missing or too-old Node. Names only the remedies the lookup
# above actually reaches: NOVEL_SYSTEM_NODE_BIN overrides every other Node, and
# nvm's default alias wins over ~/.local/node.
dev_node_install_hint() {
  if [ -n "${NOVEL_SYSTEM_NODE_BIN:-}" ]; then
    echo "   NOVEL_SYSTEM_NODE_BIN=${NOVEL_SYSTEM_NODE_BIN} overrides every other Node: point it at" >&2
    echo '   the bin/ directory of Node 22 (the version CI tests), or unset it.' >&2
  elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
    echo '   nvm is installed and its default alias wins over ~/.local/node: run' >&2
    echo '   `nvm install 22 && nvm alias default 22` (22 is the version CI tests), or point' >&2
    echo '   NOVEL_SYSTEM_NODE_BIN at the bin/ directory of a Node 22 install.' >&2
  else
    echo '   Install Node 22 (the version CI tests): unpack it at ~/.local/node, or point' >&2
    echo '   NOVEL_SYSTEM_NODE_BIN at its bin/ directory.' >&2
  fi
  echo "   CentOS 7 / glibc 2.17: use Node 22's glibc-217 build from" >&2
  echo '   https://unofficial-builds.nodejs.org/download/release/' >&2
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

#!/usr/bin/env bash
# Linux dev equivalent of start-dev.cmd's React-frontend leg.
# Safe to re-run: stops any previous instance (by pidfile, falling back to
# whatever is bound to the port) before starting a fresh one — but only after
# the toolchain checks passed, so a relaunch with a missing or too-old Node
# leaves the running frontend alone.
#
# Node resolution (dev_use_frontend_node in lib/dev-lifecycle.sh) — first hit wins:
#   1. $NOVEL_SYSTEM_NODE_BIN   directory holding node/npm (explicit override)
#   2. nvm ($NVM_DIR, ~/.nvm)   sourced; a hit when its default alias (or an
#                               already active version) puts an nvm node on PATH
#   3. ~/.local/node/bin        a plain tarball install (the Ubuntu dev host)
#   4. whatever `node` is already on PATH
# The frontend needs Node >= 18.18 (package.json "engines"); Node 22 is what CI tests.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/dev-lifecycle.sh
source "$SCRIPT_DIR/lib/dev-lifecycle.sh"

RUN_DIR="$REPO_ROOT/.codex-run"
mkdir -p "$RUN_DIR"
PID_FILE="$RUN_DIR/frontend-react.pid"
URL_FILE="$RUN_DIR/frontend-react.url"
PORT="${NOVEL_SYSTEM_FRONTEND_PORT:-5174}"

dev_use_frontend_node
dev_check_frontend_toolchain "$REPO_ROOT/frontend-react" || exit 1

dev_stop_pidfile "$PID_FILE"
dev_stop_port "$PORT"

cd "$REPO_ROOT/frontend-react"

echo "==> node $(node --version) at $(command -v node)"

echo "http://127.0.0.1:${PORT}" > "$URL_FILE"
echo $$ > "$PID_FILE"
exec npm run dev -- --host 127.0.0.1 --port "$PORT"

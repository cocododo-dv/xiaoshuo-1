#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  if [[ ! -x "$PYTHON_BIN" ]]; then
    printf 'Configured PYTHON_BIN is not executable: %s\n' "$PYTHON_BIN" >&2
    exit 2
  fi
elif [[ -x "$BACKEND_DIR/.venv-wsl/bin/python" ]]; then
  PYTHON_BIN="$BACKEND_DIR/.venv-wsl/bin/python"
elif [[ -x "$BACKEND_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
else
  while IFS= read -r line; do
    if [[ "$line" == worktree\ * ]]; then
      CANDIDATE_ROOT="${line#worktree }"
      if [[ "$CANDIDATE_ROOT" != "$REPO_ROOT" ]]; then
        for VENV_NAME in .venv-wsl .venv; do
          CANDIDATE_PYTHON="$CANDIDATE_ROOT/backend/$VENV_NAME/bin/python"
          if [[ -x "$CANDIDATE_PYTHON" ]]; then
            PYTHON_BIN="$CANDIDATE_PYTHON"
            break 2
          fi
        done
      fi
    fi
  done < <(git -C "$REPO_ROOT" worktree list --porcelain)

  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

run_step() {
  local label="$1"
  shift
  printf '==> %s\n' "$label"
  PYTHONPATH="$BACKEND_DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$@"
}

cd "$BACKEND_DIR"

printf 'Using WSL Python: %s\n' "$PYTHON_BIN"

if ! "$PYTHON_BIN" -c 'import chromadb, multipart, pytest' >/dev/null 2>&1; then
  printf '%s\n' 'WSL strict verification requires the complete dev + chroma environment.' >&2
  printf '%s\n' 'Prepare it with: cd backend && UV_PROJECT_ENVIRONMENT=.venv-wsl uv sync --locked --extra dev --extra chroma' >&2
  exit 2
fi

run_step "Chroma smoke" "$PYTHON_BIN" -m novel_system.tools.chroma_smoke
run_step "Focused Chroma suite" "$PYTHON_BIN" -m pytest tests/test_chroma_smoke.py tests/test_chroma_vector_store.py tests/test_acceptance_flow.py -q
run_step "Full backend pytest" "$PYTHON_BIN" -m pytest -q

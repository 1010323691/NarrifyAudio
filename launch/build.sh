#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
cd -- "$REPO_ROOT"

PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Create the Linux .venv and install dependencies first." >&2; exit 1; }
[[ -d node_modules ]] || { echo "Run npm ci (or npm install) first." >&2; exit 1; }
command -v npm >/dev/null || { echo "npm is required. Check PATH (user-level installs live in ~/.local/bin)." >&2; exit 1; }

# build:all (scripts/build-all.mjs) runs, in order:
#   1. npm run build          - vue-tsc typecheck + Vite production build into dist/
#   2. python -m compileall   - backend + tts-engine syntax/bytecode check (no imports, no models)
#   3. lint-imports           - the api -> services -> platform -> core layer gate
npm run build:all

cat <<'EOF'

Build complete.
Frontend: dist/ is rebuilt; the API serves it per request, so a hard browser
refresh is enough (no service restart).
Backend: Python has no build artifact; restart the units that run the changed
code to load it, e.g.
  systemctl --user restart narrify-api
  systemctl --user restart narrify-worker
(For new DB migrations also: systemctl --user restart narrify-migrate.)
EOF

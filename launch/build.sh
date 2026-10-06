#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
cd -- "$REPO_ROOT"

BUILD_ONLY=0
INSTALL_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --build-only) BUILD_ONLY=1 ;;
    --install-deps) INSTALL_DEPS=1 ;;
    --help|-h)
      echo "Usage: bash launch/build.sh [--build-only] [--install-deps]"
      echo "Build, migrate and restart the installed API/Worker services by default."
      echo "--build-only: build without changing running services."
      echo "--install-deps: run npm ci and install backend/requirements.txt first."
      exit 0 ;;
    *) echo "Unknown option: $arg (use --help)." >&2; exit 1 ;;
  esac
done
if (( BUILD_ONLY && INSTALL_DEPS )); then
  echo "--build-only and --install-deps cannot be combined: dependency updates require stopping the application." >&2
  exit 1
fi

PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Create the Linux .venv and install dependencies first." >&2; exit 1; }
command -v npm >/dev/null || { echo "npm is required. Check PATH (user-level installs live in ~/.local/bin)." >&2; exit 1; }
command -v flock >/dev/null || { echo "flock is required." >&2; exit 1; }
umask 077
mkdir -p "$REPO_ROOT/.narrify"
exec 9>"$REPO_ROOT/.narrify/dev-launch.lock"
flock -n 9 || { echo "Another launcher is active; stop dev-all.sh or wait for startup." >&2; exit 1; }

if (( ! BUILD_ONLY )); then
  require_systemd
  command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
  # Fail before building if this host has no installed application units.
  for unit in narrify-api.service narrify-worker.service narrify-migrate.service; do
    [[ "$(unitctl show "$unit" --property=LoadState --value)" == "loaded" ]] || {
      echo "$unit is not installed. Install the units per readme-linux.md, or use --build-only." >&2
      exit 1
    }
  done
fi

if (( INSTALL_DEPS )); then
  # Stop before changing the shared Python environment under running Workers.
  if (( ! BUILD_ONLY )); then
    unitctl stop narrify-api.service narrify-worker.service
  fi
  npm ci
  "$PYTHON" -m pip install -r backend/requirements.txt
  "$PYTHON" -m pip check
fi
[[ -d node_modules ]] || { echo "Run with --install-deps (or run npm ci) first." >&2; exit 1; }

# Includes frontend typecheck/build, Python compile checks and the layer gate.
# A build failure exits before any deployment action (unless installing deps).
npm run build:all
if (( BUILD_ONLY )); then
  echo "Build complete. Services were not restarted (--build-only)."
  exit 0
fi

echo "Build passed. Reloading services and stopping API/Worker..."
unitctl daemon-reload
unitctl stop narrify-api.service narrify-worker.service
unitctl start "$POSTGRES_SERVICE" "$REDIS_SERVICE"
unitctl is-active --quiet "$POSTGRES_SERVICE"
unitctl is-active --quiet "$REDIS_SERVICE"
# RemainAfterExit migration units must be restarted to apply new revisions.
# Failure intentionally leaves the application stopped, with data intact.
echo "Applying database migrations..."
unitctl restart narrify-migrate.service
echo "Starting API and Worker..."
unitctl start narrify-api.service narrify-worker.service
unitctl is-active --quiet narrify-api.service
unitctl is-active --quiet narrify-worker.service

for ((attempt = 0; attempt < 30; attempt++)); do
  if curl -fs --max-time 2 http://127.0.0.1:8642/api/health >/dev/null; then
    echo "Build and deployment complete. API and Worker are running the current code."
    echo "Frontend: http://127.0.0.1:8642 (or your Nginx address); refresh your browser."
    exit 0
  fi
  sleep 1
done
if [[ "$UNIT_MODE" == "user" ]]; then
  echo "API health check timed out. Check: journalctl --user -u narrify-api -u narrify-worker -u narrify-migrate" >&2
else
  echo "API health check timed out. Check: journalctl -u narrify-api -u narrify-worker -u narrify-migrate" >&2
fi
exit 1

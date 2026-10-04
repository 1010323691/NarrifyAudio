#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_systemd
command -v flock >/dev/null || { echo "flock is required." >&2; exit 1; }
umask 077
mkdir -p "$REPO_ROOT/.narrify"
exec 9>"$REPO_ROOT/.narrify/dev-launch.lock"
flock -n 9 || { echo "Another launcher is active; stop dev-all.sh or wait for startup." >&2; exit 1; }
# Install the narrify units first: system units per readme-linux.md, or the
# user-level units in ~/.config/systemd/user/ (auto-detected in common.sh).
# Their EnvironmentFile supplies credentials; never source .env as shell code.
bash "$LAUNCH_DIR/start-data-services.sh"
if ! unitctl is-active --quiet narrify-api.service && ! unitctl is-active --quiet narrify-worker.service; then
  unitctl restart narrify-migrate.service
fi
unitctl start narrify-api.service narrify-worker.service
unitctl is-active --quiet narrify-api.service
unitctl is-active --quiet narrify-worker.service
command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }
for ((attempt = 0; attempt < 30; attempt++)); do
  if curl -fsS --max-time 2 http://127.0.0.1:8642/api/health >/dev/null; then
    echo "API and Worker are ready. Frontend: http://127.0.0.1:8642 (built dist/), or your Nginx address."
    exit 0
  fi
  sleep 1
done
if [[ "$UNIT_MODE" == "user" ]]; then
  echo "API health check timed out. Check: journalctl --user -u narrify-api -u narrify-worker" >&2
else
  echo "API health check timed out. Check: journalctl -u narrify-api -u narrify-worker" >&2
fi
exit 1

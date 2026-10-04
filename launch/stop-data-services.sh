#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_systemd
command -v flock >/dev/null || { echo "flock is required." >&2; exit 1; }
umask 077
mkdir -p "$REPO_ROOT/.narrify"
exec 9>"$REPO_ROOT/.narrify/dev-launch.lock"
flock -n 9 || { echo "Another launcher is active; exit dev-all.sh or wait for startup." >&2; exit 1; }
for unit in narrify-api.service narrify-worker.service; do
  if systemctl is-active --quiet "$unit"; then
    echo "Stop the application first: bash launch/stop.sh" >&2
    exit 1
  fi
done
systemctl_as_admin stop "$REDIS_SERVICE" "$POSTGRES_SERVICE"
echo "PostgreSQL and Redis are stopped."

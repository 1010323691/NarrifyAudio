#!/usr/bin/env bash
# Shared helpers for Linux launchers.
set -euo pipefail
LAUNCH_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$LAUNCH_DIR/.." && pwd)"

# System-wide installs (readme-linux.md) put the units in /etc/systemd/system
# and need sudo; user-level deployments keep all five units in
# ~/.config/systemd/user/ and run them on the user bus without sudo.
# All five units must be present to be recognized as a user deployment:
# with a partial set, data-service startup would fail on missing units,
# so a partial install stays on the system-wide path (overridable via the
# POSTGRES_SERVICE / REDIS_SERVICE environment variables).
USER_UNIT_DIR="${HOME:?HOME is required}/.config/systemd/user"
USER_DEPLOYMENT=1
for unit in narrify-api narrify-worker narrify-migrate narrify-postgresql narrify-redis; do
  [[ -f "$USER_UNIT_DIR/$unit.service" ]] || { USER_DEPLOYMENT=0; break; }
done
if (( USER_DEPLOYMENT )); then
  UNIT_MODE="user"
  POSTGRES_SERVICE="${POSTGRES_SERVICE:-narrify-postgresql.service}"
  REDIS_SERVICE="${REDIS_SERVICE:-narrify-redis.service}"
else
  UNIT_MODE="system"
  POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgresql@16-main.service}"
  REDIS_SERVICE="${REDIS_SERVICE:-redis-server.service}"
fi

# The single systemctl entry point: the user bus (no sudo) for user-level
# deployments, plain/sudo systemctl for system-wide installs.
unitctl() {
  if [[ "$UNIT_MODE" == "user" ]]; then
    systemctl --user "$@"
  elif (( EUID == 0 )); then
    systemctl "$@"
  else
    sudo systemctl "$@"
  fi
}

require_systemd() {
  command -v systemctl >/dev/null || { echo "systemctl is required." >&2; exit 1; }
  if [[ "$UNIT_MODE" == "user" ]]; then
    [[ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bus" ]] || {
      echo "The user systemd bus is unavailable; run inside the user's session." >&2
      exit 1
    }
  elif [[ ! -d /run/systemd/system ]]; then
    echo "Run on Linux with systemd." >&2
    exit 1
  fi
}

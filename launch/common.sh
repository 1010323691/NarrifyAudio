#!/usr/bin/env bash
# Shared helpers for Linux launchers.
set -euo pipefail
LAUNCH_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$LAUNCH_DIR/.." && pwd)"

# System-wide installs (readme-linux.md) put the units in /etc/systemd/system
# and need sudo; user-level deployments keep all five units in
# ~/.config/systemd/user/ and run them on the user bus without sudo.
USER_UNIT_DIR="${HOME:?HOME is required}/.config/systemd/user"
if [[ -d "$USER_UNIT_DIR" ]] && compgen -G "$USER_UNIT_DIR/narrify-*.service" >/dev/null; then
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

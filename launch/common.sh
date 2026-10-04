#!/usr/bin/env bash
# Shared helpers for Linux launchers.
set -euo pipefail
LAUNCH_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$LAUNCH_DIR/.." && pwd)"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgresql@16-main.service}"
REDIS_SERVICE="${REDIS_SERVICE:-redis-server.service}"

systemctl_as_admin() {
  if (( EUID == 0 )); then
    systemctl "$@"
  else
    sudo systemctl "$@"
  fi
}

require_systemd() {
  command -v systemctl >/dev/null || { echo "systemctl is required." >&2; exit 1; }
  [[ -d /run/systemd/system ]] || { echo "Run on Linux with systemd." >&2; exit 1; }
}

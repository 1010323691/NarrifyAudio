#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_systemd
# Honor each unit's graceful shutdown timeout.
systemctl_as_admin stop narrify-worker.service narrify-api.service
echo "API and Worker are stopped. PostgreSQL and Redis remain running."

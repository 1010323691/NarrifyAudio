#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_systemd
unitctl start "$POSTGRES_SERVICE" "$REDIS_SERVICE"
unitctl is-active --quiet "$POSTGRES_SERVICE"
unitctl is-active --quiet "$REDIS_SERVICE"
echo "PostgreSQL and Redis are running."

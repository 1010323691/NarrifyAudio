#!/usr/bin/env bash
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_systemd
systemctl_as_admin start "$POSTGRES_SERVICE" "$REDIS_SERVICE"
systemctl is-active --quiet "$POSTGRES_SERVICE"
systemctl is-active --quiet "$REDIS_SERVICE"
echo "PostgreSQL and Redis are running."

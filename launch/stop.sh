#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
require_systemd
# Stop the application first so the data services shut down only once no
# consumer is left; the app-stopped guard and dev-launcher lock live in
# stop-data-services.sh and are reused here.
unitctl stop narrify-worker.service narrify-api.service
bash "$LAUNCH_DIR/stop-data-services.sh"
echo "The whole stack is stopped."

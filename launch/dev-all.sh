#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
cd -- "$REPO_ROOT"
PYTHON="$REPO_ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || { echo "Create the Linux .venv and install dependencies first." >&2; exit 1; }
[[ -d node_modules ]] || { echo "Run npm ci first." >&2; exit 1; }
for tool in npm curl flock setsid; do
  command -v "$tool" >/dev/null || { echo "$tool is required." >&2; exit 1; }
done
# Re-exec with .env parsed as data, never evaluated as shell commands.
if [[ "${1:-}" != "--env-loaded" ]]; then
  exec "$PYTHON" - "$LAUNCH_DIR/dev-all.sh" <<'PY'
import os
import re
import sys
from pathlib import Path

env = os.environ.copy()
file = Path('.env')
if not file.is_file():
    sys.exit('Missing .env. Configure database and Redis URLs first.')
for number, line in enumerate(file.read_text(encoding='utf-8-sig').splitlines(), 1):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    if '=' not in line:
        sys.exit(f'Invalid .env assignment at line {number}')
    key, value = (part.strip() for part in line.split('=', 1))
    if not re.fullmatch(r'NARRIFY_[A-Z0-9_]+', key):
        continue
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    env[key] = value
os.execvpe('bash', ['bash', sys.argv[1], '--env-loaded'], env)
PY
fi
umask 077
mkdir -p .narrify logs/dev
exec 9>.narrify/dev-launch.lock
flock -n 9 || { echo "A development launcher is already running." >&2; exit 1; }
if command -v systemctl >/dev/null; then
  for unit in narrify-api.service narrify-worker.service; do
    if unitctl is-active --quiet "$unit"; then
      echo "Stop the systemd application first: bash launch/stop.sh" >&2
      exit 1
    fi
  done
fi
# This supervisor only stops processes it creates, so reject occupied app ports.
"$PYTHON" - <<'PY'
import socket
import sys
for port in (8642, 5173):
    for host in ('127.0.0.1', '::1'):
        try:
            connection = socket.create_connection((host, port), timeout=0.3)
        except OSError:
            continue
        connection.close()
        sys.exit(f'Port {port} is occupied. Stop the existing application first.')
PY
pids=()
cleanup() {
  trap - EXIT INT TERM
  for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  for ((attempt = 0; attempt < 20; attempt++)); do
    local alive=0
    for pid in "${pids[@]}"; do
      if kill -0 -- "-$pid" 2>/dev/null; then alive=1; fi
    done
    (( alive == 0 )) && break
    sleep 0.5
  done
  for pid in "${pids[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Migrate once before launching either database consumer.
"$PYTHON" -m alembic upgrade head
setsid "$PYTHON" -m backend.main >logs/dev/api.log 2>logs/dev/api.err.log 9>&- &
pids+=("$!")
setsid "$PYTHON" -m backend.worker >logs/dev/worker.log 2>logs/dev/worker.err.log 9>&- &
pids+=("$!")
ready=0
for ((attempt = 0; attempt < 30; attempt++)); do
  for pid in "${pids[@]}"; do
    kill -0 "$pid" 2>/dev/null || { echo "API or Worker exited; check logs/dev/." >&2; exit 1; }
  done
  if curl -fsS --max-time 2 http://127.0.0.1:8642/api/health >/dev/null; then ready=1; break; fi
  sleep 1
done
(( ready == 1 )) || { echo "API health check timed out; check logs/dev/." >&2; exit 1; }
echo "Frontend: http://127.0.0.1:5173 ; Ctrl+C stops this development stack."
setsid npm run dev -- --host 127.0.0.1 9>&- &
pids+=("$!")
# Exit when any component exits, avoiding a stack with a dead Worker.
wait -n

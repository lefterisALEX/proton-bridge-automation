#!/usr/bin/env bash
# Entrypoint for the protonmail-jobs container: run the hourly maintenance on
# the hour, forever. Waits until the bridge actually answers IMAP (a plain TCP
# connect succeeds as soon as socat listens, before the daemon serves).
set -u

host="${PMB_IMAP_HOST:-protonmail-bridge}"
port="${PMB_IMAP_PORT:-143}"

imap_ready() {
    python3 - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    s = socket.create_connection((host, port), 5)
    s.settimeout(5)
    greeting = s.recv(64)
    s.close()
except OSError:
    sys.exit(1)
sys.exit(0 if greeting.startswith(b"* OK") else 1)
PY
}

echo "waiting for bridge IMAP at ${host}:${port} ..."
until imap_ready; do sleep 5; done
echo "bridge IMAP ready"

while :; do
    /app/scripts/run_hourly.sh || echo "run_hourly.sh exited $?"
    now="$(date +%s)"
    sleep $(( 3600 - now % 3600 ))
done

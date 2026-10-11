#!/usr/bin/env bash
# Entrypoint for the protonmail-jobs container: run the hourly maintenance on
# the hour, forever. Waits for the bridge to accept connections first.
set -u

host="${PMB_IMAP_HOST:-protonmail-bridge}"
port="${PMB_IMAP_PORT:-143}"

echo "waiting for bridge at ${host}:${port} ..."
until (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; do
    sleep 5
done
echo "bridge reachable"

while :; do
    /app/scripts/run_hourly.sh || echo "run_hourly.sh exited $?"
    now="$(date +%s)"
    sleep $(( 3600 - now % 3600 ))
done

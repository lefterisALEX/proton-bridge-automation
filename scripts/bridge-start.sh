#!/usr/bin/env bash
# Start the Bridge daemon in the background (after the one-time login).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose up -d
echo "Bridge is running. IMAP: 127.0.0.1:1143, SMTP: 127.0.0.1:1025"
docker compose logs --tail=20 protonmail-bridge
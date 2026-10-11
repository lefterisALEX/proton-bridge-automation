#!/usr/bin/env bash
# One-time interactive login for the Proton Mail Bridge container.
# You will be dropped into the bridge CLI. Do:
#   login   -> follow the prompts (email, password, 2FA if enabled)
#   info    -> write down the username and mailbox password shown
#   exit    -> back out of the CLI, then Ctrl+C to stop the container
set -euo pipefail
exec docker run --rm -it -v protonmail:/root dancwilliams/protonmail-bridge init
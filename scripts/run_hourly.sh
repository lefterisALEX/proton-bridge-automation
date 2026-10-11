#!/usr/bin/env bash
# Hourly Proton Mail maintenance, run by the protonmail-jobs container
# (scripts/job-loop.sh) on the hour.
#
# Order matters: archive labeled mail, sweep unlabeled mail, then drain the
# forwarding queue. The "forward" label is protected in labeled_mover (via
# PMB_KEEP_LABELS) so queued mail is not archived before it is forwarded.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

log() { printf '%s %s\n' "$(date -Is)" "$*"; }

log "labeled_mover: archiving labeled INBOX mail"
./python/labeled_mover.py --move

log "unlabeled_mover: sweeping unlabeled INBOX mail"
./python/unlabeled_mover.py --move

log "erasmus_forwarder: draining the forward queue"
./python/erasmus_forwarder.py --send

log "done"

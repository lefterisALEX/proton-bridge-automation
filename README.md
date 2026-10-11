# proton-bridge-automation

Dockerized Proton Mail Bridge with hourly label-driven mail movers and a
Sieve-queued auto-forwarder.

Moves emails that sit in INBOX **without any label** into a folder
(`Folders/Unlabeled` by default), archives labeled mail, and forwards the mail
your Sieve filters drop into a queue label. Emails that carry a normal label are
left in place. Safely dry-runs by default.

- `python/unlabeled_mover.py` — the mover (pure Python stdlib, no dependencies)
- `python/labeled_mover.py` — moves labeled INBOX mail to `Archive` (except protected labels)
- `python/erasmus_forwarder.py` — drains a Sieve-applied label queue, forwarding each message and removing the label
- `sieve/` — **private submodule** ([`lefterisALEX/proton-bridge-sieve`](https://github.com/lefterisALEX/proton-bridge-sieve)):
  your personal Sieve filters (`labels.sieve`, `expiration.sieve`,
  `trash-newsletters.sieve`). Not part of this public repo; needs access to clone.
- `docker-compose.yml` — runs Proton Mail Bridge (`dancwilliams/protonmail-bridge`, multi-arch) + the hourly job container
- `Dockerfile.jobs` — image for the periodic job (runs all three scripts hourly)
- `scripts/run_hourly.sh` — the hourly sequence (labeled mover, unlabeled mover, forwarder)
- `scripts/job-loop.sh` — container entrypoint that runs the sequence on the hour
- `scripts/bridge-init.sh` — one-time interactive login to the bridge
- `scripts/bridge-start.sh` — start the bridge daemon
- `.env.example` — copy to `.env` and fill in

## Keeping the sieve filters in sync

Your filters live in `sieve/labels.sieve` and `sieve/expiration.sieve` (a private
submodule — commit/push changes from inside `sieve/`). After
editing them here, apply each in the web app (Settings -> Filters -> the filter
containing these rules -> replace the Sieve source -> Save) — there is no API
to push filters to Proton. **Order matters**: filters run top-to-bottom in the
web app and each matching rule stops. Keep the labels filter **above** the
expiration filter so labeled mail is never expired by mistake. The mover relies
on the labels your filters assign: messages that carry one of those labels stay
in INBOX, everything else older than `PMB_MIN_AGE_HOURS` is moved out. If you
change the filters in the app, update these files too so they don't drift.

## How label detection works

A message is "labeled" when it appears in any IMAP mailbox under `Labels/`
(the bridge exposes labels as mailboxes, confirmed in the bridge's own test
suite). Because UIDs are per-mailbox, messages are correlated across mailboxes
by their `Message-ID` header (with `X-Pm-Gluon-Id` as a fallback).

A minimum age (default 24 h) keeps messages that are too fresh from being
moved, giving your Sieve filters and the bridge sync time to apply labels
first.

## Set up on a new machine

Requirements:

- Docker Engine + Docker Compose v2
- A paid Proton plan (Bridge and Sieve filters both require one)
- This repo (git clone, or copy the folder over)

### 1. Get the repo

```bash
git clone --recurse-submodules https://github.com/lefterisALEX/proton-bridge-automation.git protonmail
cd protonmail

# If you already cloned without the flag, fetch the private sieve submodule:
# git submodule update --init
```

### 2. One-time bridge login (interactive)

```bash
./scripts/bridge-init.sh
```

In the bridge CLI that appears:

```
login   -> your Proton email + password (+ 2FA if enabled)
info    -> write down the IMAP/SMTP username and mailbox password it prints
exit    -> leave the CLI (then Ctrl+C to stop the container)
```

This stores the session in the `protonmail` Docker volume, so you do it once per
machine. To **migrate an existing login** to a new machine, copy that volume
(`docker volume inspect protonmail` for its path) instead of re-running `login`.

### 3. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

- `PMB_USER` — the **username** shown by `info`
- `PMB_PASSWORD` — the **mailbox password** shown by `info` (not your Proton account password)
- `PMB_FORWARD_TO` — where forwarded mail should go (optional; override with `--to`)
- optionally `PMB_KEEP_LABELS`, `PMB_MIN_AGE_HOURS`, `PMB_TARGET`

### 4. Start the bridge and the hourly job

```bash
docker compose up -d
docker compose ps                 # protonmail-bridge should report (healthy)
docker logs -f protonmail-jobs    # first run happens immediately, then hourly
```

The bridge image is pulled automatically (multi-arch). Ports are published to
`127.0.0.1` only. The `protonmail-jobs` container then runs the hourly sequence
and talks to the bridge over the internal compose network.

### 5. Apply the Sieve filters (web app, one-time)

Settings -> All settings -> Proton Mail -> Filters. Paste each of
`sieve/labels.sieve`, `sieve/expiration.sieve`, and
`sieve/trash-newsletters.sieve` as a separate Sieve filter, and keep the
**labels filter above the expiration filter**. See
[Keeping the sieve filters in sync](#keeping-the-sieve-filters-in-sync).

### 6. Verify

```bash
# Dry runs from the host (run from the repo root; use the published 127.0.0.1
# ports from .env):
./python/unlabeled_mover.py   # what would move out of INBOX
./python/labeled_mover.py     # what would be archived
./python/erasmus_forwarder.py # what would be forwarded

# Then run for real, or just let the hourly container do it:
./python/unlabeled_mover.py --move
./python/labeled_mover.py --move
./python/erasmus_forwarder.py --send
```

Done — the `protonmail-jobs` container now handles the movers and the forwarder
every hour. Options: `--help` lists everything; key ones are `--move`/`--send`,
`--min-age-hours N`, `--target 'Folders/Elsewhere'`, `--env-file path`.

## Auto-forwarding queued mail (`python/erasmus_forwarder.py`)

Proton's Sieve/filters cannot forward. To forward a subset of mail (e.g. all
`erasmusmc.nl`), have the Sieve filter apply an extra label (default `forward`)
to the matching messages, then let this script drain that label:

```bash
# Dry run: shows what would be forwarded.
./python/erasmus_forwarder.py --to destination@example.com

# Actually forward and remove the "forward" label afterwards.
./python/erasmus_forwarder.py --to destination@example.com --send
```

The label is the queue: a message is forwarded only if it is in INBOX **and**
carries the label, and the label is removed after a successful send, so re-runs
only process new arrivals (old mail is never touched — Sieve only labels new
mail). Messages are forwarded in full (original attached as `message/rfc822`),
sent through the bridge's SMTP port (`PMB_SMTP_PORT`, default 1025). The `From`
must be one of your own addresses (`PMB_FORWARD_FROM`, defaults to `PMB_USER`).

Configure with `PMB_FORWARD_TO`, `PMB_FORWARD_LABEL`, `PMB_SMTP_HOST`,
`PMB_SMTP_PORT`, `PMB_FORWARD_FROM`, or the matching CLI flags. The queue label
is created automatically once `sieve/labels.sieve` references it.

## Periodic job (Docker)

`protonmail-jobs` runs the hourly sequence inside the compose network, so it
needs neither host cron nor an active login session. It talks to the bridge over
the internal ports (`protonmail-bridge:143` IMAP, `protonmail-bridge:25` SMTP),
which the bridge's socat helpers forward to its local 1143/1025.

`scripts/run_hourly.sh` runs, in order:

1. `python/labeled_mover.py --move` — archives labeled INBOX mail
2. `python/unlabeled_mover.py --move` — sweeps unlabeled INBOX mail
3. `python/erasmus_forwarder.py --send` — drains the `forward` queue

The `forward` label is listed in `PMB_KEEP_LABELS`, so step 1 does not archive
queued mail before step 3 forwards it.

```bash
docker compose up -d --build protonmail-jobs   # start (runs immediately, then hourly)
docker logs -f protonmail-jobs                 # watch it
docker compose restart protonmail-jobs         # force an immediate run
```

The repo is bind-mounted over `/app`, so edited scripts take effect on the next
run with no rebuild. Config comes from `.env` (via `env_file`); the compose file
overrides the host-side `PMB_IMAP_HOST`/`PMB_SMTP_HOST` with the in-network
bridge addresses, so changing `.env` needs `docker compose up -d` (recreate).

## Running on a Raspberry Pi

The bridge runs the multi-arch community image
[`dancwilliams/protonmail-bridge`](https://github.com/dancwilliams/protonmail-bridge-docker)
(built from source): `linux/amd64`, `linux/arm64/v8`, and `riscv64`. On a Pi
with a 64-bit OS (`uname -m` reports `aarch64`) it runs natively — just
`docker compose up -d`. 32-bit `arm/v7` is not supported by either project.
`protonmail-jobs` (`python:3.12-slim`) is multi-arch too, so the whole stack
works on the Pi; total memory is ~18 MiB.

## Notes

- Proton folder handlings: moving a message **to a folder** removes it from
  INBOX; moving it **to a label** keeps it in INBOX.
- Message expiration is applied by Proton server-side via your Sieve filter;
  it is not visible to IMAP and is untouched by this script.
- `--min-age-hours` protects against the small window where the bridge has
  fetched a brand-new message but the Sieve-applied label has not synced yet.
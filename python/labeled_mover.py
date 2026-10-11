#!/usr/bin/env python3
"""Move labeled emails out of INBOX into the archive via Proton Mail Bridge IMAP.

The inverse of unlabeled_mover.py: messages in INBOX that carry at least one
label (i.e. they appear in a mailbox under the label root, by default
"Labels") are moved to the archive.  Messages carrying one of the protected
labels (by default newsletters and forward) are left in INBOX.

Safe by default: without --move the script only reports what it would do.

Configuration order of precedence: command-line flags > environment variables
(see below) > built-in defaults.  A simple KEY=VALUE "env file" can be passed
with --env-file (blank lines and # comments are ignored).

Environment variables:
    PMB_IMAP_HOST      IMAP host of the bridge        (default: 127.0.0.1)
    PMB_IMAP_PORT      IMAP port of the bridge        (default: 1143)
    PMB_USER           Bridge mailbox username (email address)
    PMB_PASSWORD       Bridge mailbox password
    PMB_ARCHIVE        Folder to move labeled mail into (default: Archive)
    PMB_LABEL_ROOT     IMAP root under which labels live (default: Labels)
    PMB_KEEP_LABELS    Comma-separated labels to leave in INBOX
                       (default: newsletters,forward)
    PMB_MIN_AGE_HOURS  Only move messages older than this many hours (default: 0)
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import logging
import os
import sys

import unlabeled_mover as um

log = logging.getLogger("labeled_mover")


def parse_flags(
    args: argparse.Namespace, env: dict[str, str]
) -> tuple[str, int, str, str, str, str, set[str], float]:
    host = args.host or env.get("PMB_IMAP_HOST", "127.0.0.1")
    port = args.port or int(env.get("PMB_IMAP_PORT", "1143"))
    user = args.user or env.get("PMB_USER", "")
    password = args.password or env.get("PMB_PASSWORD", "")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as fh:
            password = fh.read().strip()
    target = args.target or env.get("PMB_ARCHIVE", "Archive")
    label_root = args.label_root or env.get("PMB_LABEL_ROOT", "Labels")
    keep_labels = args.keep_labels or env.get("PMB_KEEP_LABELS", "newsletters,forward")
    keep = {name.strip().lower() for name in keep_labels.split(",") if name.strip()}
    min_age_hours = args.min_age_hours if args.min_age_hours is not None else float(env.get("PMB_MIN_AGE_HOURS", "0"))
    return host, port, user, password, target, label_root, keep, min_age_hours


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Move labeled emails out of INBOX into the archive via Proton Mail Bridge.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", help="Bridge IMAP host (env: PMB_IMAP_HOST)")
    p.add_argument("--port", type=int, help="Bridge IMAP port (env: PMB_IMAP_PORT)")
    p.add_argument("--user", help="Bridge mailbox username (env: PMB_USER)")
    p.add_argument("--password", help="Bridge mailbox password (env: PMB_PASSWORD)")
    p.add_argument("--password-file", help="Read the mailbox password from a file")
    p.add_argument("--target", help="Folder to move labeled mail into (env: PMB_ARCHIVE)")
    p.add_argument(
        "--label-root",
        help="IMAP prefix under which user labels are exposed (env: PMB_LABEL_ROOT)",
    )
    p.add_argument(
        "--keep-labels",
        help="Comma-separated labels to leave in INBOX (env: PMB_KEEP_LABELS)",
    )
    p.add_argument(
        "--min-age-hours",
        type=float,
        help="Only move messages older than this many hours (env: PMB_MIN_AGE_HOURS)",
    )
    p.add_argument(
        "--env-file",
        help="Load configuration from a KEY=VALUE file (default: .env if present)",
    )
    p.add_argument(
        "--move",
        action="store_true",
        help="Actually move messages (default is a dry run)",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase log verbosity")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    env_file = args.env_file or um.find_env_file()
    env = um.load_env_file(env_file)

    host, port, user, password, target, label_root, keep, min_age_hours = parse_flags(args, env)

    if not user:
        user = input("Bridge username (email address): ").strip()
    if not password:
        password = getpass.getpass("Bridge mailbox password: ")

    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    log.info("connecting to %s:%s as %s", host, port, user)
    M = um.connect(host, port, user, password)
    try:
        mailboxes = um.list_mailboxes(M)
        label_mailboxes = [m for m in mailboxes if m.startswith(label_root + "/")]
        log.info("label mailboxes: %s", ", ".join(label_mailboxes) or "(none)")

        inbox_ids, inbox_dates = um.fetch_metadata(M, "INBOX")

        labels_by_mid: dict[bytes, set[str]] = {}
        for mbox in label_mailboxes:
            label = mbox[len(label_root) + 1 :]
            ids, _ = um.fetch_metadata(M, mbox)
            for mid in ids.values():
                labels_by_mid.setdefault(mid, set()).add(label)

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=min_age_hours)
        candidates = []
        for uid, mid in inbox_ids.items():
            labels = labels_by_mid.get(mid)
            if not labels:
                continue
            message_date = inbox_dates.get(uid)
            if message_date is None:
                log.warning("UID %s in INBOX: cannot determine date, leaving in place", uid)
                continue
            if message_date > cutoff:
                log.info("UID %s in INBOX: message younger than %s h, skipping", uid, min_age_hours)
                continue
            if any(label.lower() in keep for label in labels):
                log.info(
                    "UID %s in INBOX: protected label(s) %s, keeping",
                    uid,
                    ", ".join(sorted(labels)),
                )
                continue
            candidates.append((uid, mid, labels))

        if not candidates:
            log.warning("no labeled messages found in INBOX to archive")
            return 0

        log.warning("found %d labeled message(s) in INBOX to move to %s", len(candidates), target)
        if args.move and target not in mailboxes:
            log.info("creating target mailbox %s", target)
            typ, _ = M.create(um.quote_mailbox(target))
            if typ != "OK":
                log.error("failed to create %s: %s", target, typ)
                return 2
            mailboxes.append(target)

        um.select(M, "INBOX")
        moved = 0
        failed = 0
        for uid, mid, labels in candidates:
            if args.move:
                ok = um.move_message(M, uid, target)
                status = "moved" if ok else "FAILED"
                moved += int(ok)
                failed += int(not ok)
            else:
                status = "would move (dry run)"
            log.warning(
                "UID %-8s %s -> %s (labels: %s, Message-ID: %s)",
                uid,
                status,
                target,
                ",".join(sorted(labels)),
                mid.decode("latin-1", "replace"),
            )

        log.warning("done: %d moved, %d failed%s", moved, failed, "" if args.move else " (dry run)")
        return 1 if failed else 0
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
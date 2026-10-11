#!/usr/bin/env python3
"""Move unlabeled emails out of INBOX via Proton Mail Bridge IMAP.

A message is considered "labeled" when it appears in any mailbox under the
label root (by default "Labels") exposed by the bridge.  Everything left in
INBOX that carries no label is moved to the target folder.

Safe by default: without --move the script only reports what it would do.

Configuration order of precedence: command-line flags > environment variables
(see below) > built-in defaults.  A simple KEY=VALUE "env file" can be passed
with --env-file (blank lines and # comments are ignored).

Environment variables:
    PMB_IMAP_HOST      IMAP host of the bridge        (default: 127.0.0.1)
    PMB_IMAP_PORT      IMAP port of the bridge        (default: 1143)
    PMB_USER           Bridge mailbox username (email address)
    PMB_PASSWORD       Bridge mailbox password
    PMB_TARGET         Target folder for unlabeled mail (default: Folders/Unlabeled)
    PMB_LABEL_ROOT     IMAP root under which labels live (default: Labels)
    PMB_MIN_AGE_HOURS  Only move messages older than this many hours, so your
                       sieve filters and the bridge sync land first (default: 24)
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import imaplib
import logging
import os
import re
import sys
import time

log = logging.getLogger("unlabeled_mover")

LIST_ENTRY = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+(?P<delim>\"?[^\s\"]*\"?|NIL)\s+(?P<name>.*)$"
)
HEADER_MESSAGE_ID = re.compile(rb"^[Mm]essage-[Ii][Dd]:\s*(?P<value>[^\r\n]+)", re.MULTILINE)
HEADER_GLUON_ID = re.compile(rb"^X-Pm-Gluon-Id:\s*(?P<value>[^\r\n]+)", re.MULTILINE | re.IGNORECASE)
UID_IN_RESPONSE = re.compile(rb"UID (?P<uid>\d+)")

IMAP_DATE_FORMAT = "%d-%b-%Y %H:%M:%S %z"


def quote_mailbox(name: str) -> str:
    """Always quote a mailbox name; imaplib under-quotes '[' and ']' which
    Proton mailbox names (e.g. "Labels/[Imap]-Trash") need."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_list(data) -> list[str]:
    """Parse raw LIST responses into mailbox names.

    Each response line looks like:  (\\HasNoChildren) "/" "Labels/News"
    """
    names: list[str] = []
    for line in data:
        if not isinstance(line, (bytes, bytearray)):
            continue
        m = LIST_ENTRY.match(bytes(line).strip())
        if not m:
            continue
        raw = m.group("name").strip()
        if raw.startswith(b'"'):
            raw = raw[1:-1].replace(b'""', b'"')
        names.append(raw.decode("utf-8", "replace"))
    return names


def connect(
    host: str, port: int, user: str, password: str, attempts: int = 3, delay: float = 5.0
) -> imaplib.IMAP4:
    """Connect and log in, retrying briefly (the bridge may still be starting)."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            M = imaplib.IMAP4(host, port)
            M.login(user, password)
            return M
        except (imaplib.IMAP4.error, OSError) as exc:
            last = exc
            if attempt < attempts:
                log.warning("connect to %s:%s failed (%s); retry %d/%d in %ss", host, port, exc, attempt, attempts, delay)
                time.sleep(delay)
    assert last is not None
    raise last


def select(M: imaplib.IMAP4, mailbox: str) -> int:
    typ, data = M.select(quote_mailbox(mailbox))
    if typ != "OK":
        raise RuntimeError(f"cannot select {mailbox!r}: {typ} {data!r}")
    return int(data[0])


def fetch_metadata(M: imaplib.IMAP4, mailbox: str) -> tuple[dict[int, bytes], dict[int, dt.datetime]]:
    """Fetch {UID -> correlation id} and {UID -> INTERNALDATE} for a mailbox.

    The correlation id is the Message-ID header, falling back to the bridge's
    internal X-Pm-Gluon-Id header (both are stable across mailboxes while UIDs
    are not).
    """
    if select(M, mailbox) == 0:
        return {}, {}

    parts = "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-PM-GLUON-ID)])"
    typ, data = M.uid("fetch", "1:*", parts)
    if typ != "OK":
        raise RuntimeError(f"UID FETCH failed in {mailbox!r}: {typ} {data!r}")

    ids: dict[int, bytes] = {}
    dates: dict[int, dt.datetime] = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        resp, literal = item
        uid_m = UID_IN_RESPONSE.search(resp)
        if not uid_m:
            continue
        uid = int(uid_m.group("uid"))
        mid = extract_correlation_id(literal)
        if mid is not None:
            ids[uid] = mid
        raw_date = re.search(rb'INTERNALDATE "([^"]+)"', resp)
        if raw_date:
            try:
                parsed = dt.datetime.strptime(raw_date.group(1).decode(), IMAP_DATE_FORMAT)
            except ValueError:
                pass
            else:
                dates[uid] = parsed.replace(tzinfo=dt.timezone.utc)
    return ids, dates


def extract_correlation_id(literal: bytes) -> bytes | None:
    for pattern in (HEADER_MESSAGE_ID, HEADER_GLUON_ID):
        m = pattern.search(literal)
        if m:
            return m.group("value").strip().lower()
    return None


def list_mailboxes(M: imaplib.IMAP4) -> list[str]:
    typ, data = M.list()
    if typ != "OK":
        raise RuntimeError(f"LIST failed: {typ} {data!r}")
    return parse_list(data)


def parse_flags(args: argparse.Namespace, env: dict[str, str]) -> tuple[str, int, str, str, str, str, float]:
    host = args.host or env.get("PMB_IMAP_HOST", "127.0.0.1")
    port = args.port or int(env.get("PMB_IMAP_PORT", "1143"))
    user = args.user or env.get("PMB_USER", "")
    password = args.password or env.get("PMB_PASSWORD", "")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as fh:
            password = fh.read().strip()
    target = args.target or env.get("PMB_TARGET", "Folders/Unlabeled")
    label_root = args.label_root or env.get("PMB_LABEL_ROOT", "Labels")
    min_age_hours = args.min_age_hours if args.min_age_hours is not None else float(env.get("PMB_MIN_AGE_HOURS", "24"))
    return host, port, user, password, target, label_root, min_age_hours


def load_env_file(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    # Real environment variables take precedence over the env file, so the same
    # scripts can be driven by a container's environment (see .env.example).
    env.update(os.environ)
    return env


def find_env_file() -> str | None:
    """Locate a .env, checked in the CWD, this dir, then the repo root.

    Lets the scripts run from the repo root, from python/, or elsewhere without
    needing --env-file.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for directory in (os.getcwd(), here, os.path.dirname(here)):
        candidate = os.path.join(directory, ".env")
        if os.path.exists(candidate):
            return candidate
    return None


def move_message(M: imaplib.IMAP4, uid: int, target: str) -> bool:
    """Move a single message (given its UID in the currently selected mailbox).

    target must be passed pre-quoted because imaplib does not quote MOVE args.
    Uses UID MOVE; falls back to COPY + STORE \\Deleted + EXPUNGE.
    """
    quoted = '"' + target.replace("\\", "\\\\").replace('"', '\\"') + '"'
    typ, data = M.uid("move", str(uid), quoted)
    if typ == "OK":
        return True

    typ, _ = M.uid("copy", str(uid), target)
    if typ != "OK":
        return False
    typ, _ = M.uid("store", str(uid), "+FLAGS", r"(\Deleted)")
    if typ != "OK":
        return False
    M.expunge()
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Move unlabeled emails out of INBOX via Proton Mail Bridge.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", help="Bridge IMAP host (env: PMB_IMAP_HOST)")
    p.add_argument("--port", type=int, help="Bridge IMAP port (env: PMB_IMAP_PORT)")
    p.add_argument("--user", help="Bridge mailbox username (env: PMB_USER)")
    p.add_argument("--password", help="Bridge mailbox password (env: PMB_PASSWORD)")
    p.add_argument("--password-file", help="Read the mailbox password from a file")
    p.add_argument("--target", help="Folder to move unlabeled mail into (env: PMB_TARGET)")
    p.add_argument(
        "--label-root",
        help="IMAP prefix under which user labels are exposed (env: PMB_LABEL_ROOT)",
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

    env_file = args.env_file or find_env_file()
    env = load_env_file(env_file)

    host, port, user, password, target, label_root, min_age_hours = parse_flags(args, env)

    if not user:
        user = input("Bridge username (email address): ").strip()
    if not password:
        password = getpass.getpass("Bridge mailbox password: ")

    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    log.info("connecting to %s:%s as %s", host, port, user)
    M = connect(host, port, user, password)
    try:
        mailboxes = list_mailboxes(M)
        label_mailboxes = [m for m in mailboxes if m.startswith(label_root + "/")]
        log.info("label mailboxes: %s", ", ".join(label_mailboxes) or "(none)")

        inbox_ids, inbox_dates = fetch_metadata(M, "INBOX")

        labeled_ids: set[bytes] = set()
        for mbox in label_mailboxes:
            ids, _ = fetch_metadata(M, mbox)
            labeled_ids.update(ids.values())

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=min_age_hours)
        candidates = []
        for uid, mid in inbox_ids.items():
            message_date = inbox_dates.get(uid)
            if message_date is None:
                log.warning("UID %s in INBOX: cannot determine date, leaving in place", uid)
                continue
            if message_date > cutoff:
                log.info("UID %s in INBOX: message younger than %s h, skipping", uid, min_age_hours)
                continue
            if mid in labeled_ids:
                log.info("UID %s in INBOX: labeled, keeping", uid)
                continue
            candidates.append((uid, mid))

        if not candidates:
            log.warning("no unlabeled messages found in INBOX")
            return 0

        log.warning("found %d unlabeled message(s) in INBOX to move to %s", len(candidates), target)
        if args.move and target not in mailboxes:
            log.info("creating target mailbox %s", target)
            typ, _ = M.create(quote_mailbox(target))
            if typ != "OK":
                log.error("failed to create %s: %s", target, typ)
                return 2
            mailboxes.append(target)

        select(M, "INBOX")
        moved = 0
        failed = 0
        for uid, mid in candidates:
            if args.move:
                ok = move_message(M, uid, target)
                status = "moved" if ok else "FAILED"
                moved += int(ok)
                failed += int(not ok)
            else:
                status = "would move (dry run)"
            log.warning("UID %-8s %s -> %s (Message-ID: %s)", uid, status, target, mid.decode("latin-1", "replace"))

        log.warning("done: %d moved, %d failed%s", moved, failed, "" if args.move else " (dry run)")
        return 1 if failed else 0
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
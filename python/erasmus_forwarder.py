#!/usr/bin/env python3
"""Auto-forward queued mail out of Proton via the Mail Bridge, driven by a label.

The Sieve label filter files matching mail into a dedicated label (default
"forward") in addition to its normal label.  This script treats that label as a
queue: every message in INBOX that carries the label is forwarded to a target
address, and then the label is removed from the message so the next run does not
forward it again.  Old mail is never touched -- Sieve only labels new arrivals.

Forwarding uses the bridge's SMTP port.  The bridge requires the From header to
be one of your own addresses, so we send a real forward (original attached as
message/rfc822) rather than a raw redirect; this preserves attachments and
avoids DMARC problems at the destination.

Safe by default: without --send the script only reports what it would do.

Environment variables:
    PMB_IMAP_HOST     IMAP host of the bridge        (default: 127.0.0.1)
    PMB_IMAP_PORT     IMAP port of the bridge        (default: 1143)
    PMB_USER          Bridge mailbox username (email address)
    PMB_PASSWORD      Bridge mailbox password
    PMB_LABEL_ROOT    IMAP root under which labels live (default: Labels)
    PMB_FORWARD_LABEL Queue label to drain            (default: forward)
    PMB_FORWARD_TO    Destination address             (required)
    PMB_FORWARD_FROM  From address, must be yours     (default: PMB_USER)
    PMB_SMTP_HOST     SMTP host of the bridge         (default: 127.0.0.1)
    PMB_SMTP_PORT     SMTP port of the bridge         (default: 1025)
    PMB_SOURCE        Mailbox scanned for the queue   (default: INBOX)
"""

from __future__ import annotations

import argparse
import email
import getpass
import imaplib
import logging
import os
import smtplib
import sys
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import unlabeled_mover as um

log = logging.getLogger("erasmus_forwarder")


def fetch_raw(M: imaplib.IMAP4, mailbox: str, uid: int) -> bytes:
    """Return the full raw RFC822 message for a UID in mailbox."""
    um.select(M, mailbox)
    typ, data = M.uid("fetch", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError(f"UID FETCH failed in {mailbox!r}: {typ} {data!r}")
    for item in data:
        if isinstance(item, tuple):
            return item[1]
    raise RuntimeError(f"no body returned for UID {uid} in {mailbox!r}")


def remove_label(M: imaplib.IMAP4, mailbox: str, uid: int) -> bool:
    """Remove a message from a label mailbox without touching other mailboxes.

    In the bridge a label is a mailbox sharing the message with INBOX; expunging
    it here drops the label and leaves the message elsewhere.
    """
    um.select(M, mailbox)
    typ, _ = M.uid("store", str(uid), "+FLAGS", r"(\Deleted)")
    if typ != "OK":
        return False
    typ, _ = M.uid("expunge", str(uid))
    if typ != "OK":
        typ, _ = M.expunge()
    return typ == "OK"


def build_forward(raw: bytes, from_addr: str, to_addr: str) -> bytes:
    orig = email.message_from_bytes(raw)
    subject = orig.get("Subject", "(no subject)")
    outer = MIMEMultipart()
    outer["From"] = from_addr
    outer["To"] = to_addr
    outer["Subject"] = f"Fwd: {subject}"
    outer.attach(MIMEText("Forwarded automatically from a labeled message.\n", "plain"))
    outer.attach(MIMEMessage(orig))
    return outer.as_bytes()


def send_message(
    host: str, port: int, user: str, password: str, from_addr: str, to_addr: str, raw: bytes
) -> None:
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.ehlo()
        try:
            s.starttls()
            s.ehlo()
        except smtplib.SMTPException:
            log.info("SMTP STARTTLS not offered, continuing without it")
        s.login(user, password)
        s.sendmail(from_addr, [to_addr], raw)


def parse_flags(
    args: argparse.Namespace, env: dict[str, str]
) -> tuple[str, int, str, str, str, str, str, str, str, int, str]:
    host = args.host or env.get("PMB_IMAP_HOST", "127.0.0.1")
    port = args.port or int(env.get("PMB_IMAP_PORT", "1143"))
    user = args.user or env.get("PMB_USER", "")
    password = args.password or env.get("PMB_PASSWORD", "")
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as fh:
            password = fh.read().strip()
    label_root = args.label_root or env.get("PMB_LABEL_ROOT", "Labels")
    forward_label = args.forward_label or env.get("PMB_FORWARD_LABEL", "forward")
    to_addr = args.to or env.get("PMB_FORWARD_TO", "")
    from_addr = args.from_addr or env.get("PMB_FORWARD_FROM", "") or user
    smtp_host = args.smtp_host or env.get("PMB_SMTP_HOST", "127.0.0.1")
    smtp_port = args.smtp_port or int(env.get("PMB_SMTP_PORT", "1025"))
    source = args.source or env.get("PMB_SOURCE", "INBOX")
    return host, port, user, password, label_root, forward_label, to_addr, from_addr, smtp_host, smtp_port, source


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Forward mail queued in a label to another address via the Proton Mail Bridge.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", help="Bridge IMAP host (env: PMB_IMAP_HOST)")
    p.add_argument("--port", type=int, help="Bridge IMAP port (env: PMB_IMAP_PORT)")
    p.add_argument("--user", help="Bridge mailbox username (env: PMB_USER)")
    p.add_argument("--password", help="Bridge mailbox password (env: PMB_PASSWORD)")
    p.add_argument("--password-file", help="Read the mailbox password from a file")
    p.add_argument("--label-root", help="IMAP prefix under which labels live (env: PMB_LABEL_ROOT)")
    p.add_argument("--forward-label", help="Queue label to drain (env: PMB_FORWARD_LABEL)")
    p.add_argument("--to", help="Destination address (env: PMB_FORWARD_TO)")
    p.add_argument("--from-addr", help="From address, must be yours (env: PMB_FORWARD_FROM)")
    p.add_argument("--smtp-host", help="Bridge SMTP host (env: PMB_SMTP_HOST)")
    p.add_argument("--smtp-port", type=int, help="Bridge SMTP port (env: PMB_SMTP_PORT)")
    p.add_argument("--source", help="Mailbox to scan for the queue (env: PMB_SOURCE)")
    p.add_argument("--env-file", help="Load configuration from a KEY=VALUE file (default: .env if present)")
    p.add_argument("--send", action="store_true", help="Actually forward and remove the label (default is a dry run)")
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase log verbosity")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    env_file = args.env_file or um.find_env_file()
    env = um.load_env_file(env_file)

    (host, port, user, password, label_root, forward_label, to_addr,
     from_addr, smtp_host, smtp_port, source) = parse_flags(args, env)

    if not user:
        user = input("Bridge username (email address): ").strip()
    if not password:
        password = getpass.getpass("Bridge mailbox password: ")
    if not to_addr:
        log.error("no destination address; set PMB_FORWARD_TO or --to")
        return 2

    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)

    queue_mbox = f"{label_root}/{forward_label}"
    log.info("connecting to %s:%s as %s", host, port, user)
    M = um.connect(host, port, user, password)
    try:
        mailboxes = um.list_mailboxes(M)
        if queue_mbox not in mailboxes:
            log.warning("queue label %s does not exist; nothing to do", queue_mbox)
            return 0

        inbox_ids, _ = um.fetch_metadata(M, source)
        queue_ids, _ = um.fetch_metadata(M, queue_mbox)
        queue_by_id = {cid: uid for uid, cid in queue_ids.items()}
        if not queue_by_id:
            log.warning("no messages queued in %s", queue_mbox)
            return 0

        candidates = [
            (uid, cid, queue_by_id[cid])
            for uid, cid in inbox_ids.items()
            if cid in queue_by_id
        ]
        if not candidates:
            log.warning("no queued messages in %s", source)
            return 0

        log.warning("found %d queued message(s) to forward to %s", len(candidates), to_addr)
        sent = 0
        failed = 0
        for uid, cid, queue_uid in candidates:
            label = cid.decode("latin-1", "replace")
            if not args.send:
                log.warning("UID %-8s would forward -> %s (%s)", uid, to_addr, label)
                continue
            try:
                raw = fetch_raw(M, source, uid)
                fwd = build_forward(raw, from_addr, to_addr)
                send_message(smtp_host, smtp_port, user, password, from_addr, to_addr, fwd)
            except Exception as exc:  # noqa: BLE001
                log.error("UID %s: send failed: %s", uid, exc)
                failed += 1
                continue
            if remove_label(M, queue_mbox, queue_uid):
                log.warning("UID %-8s forwarded -> %s (%s)", uid, to_addr, label)
                sent += 1
            else:
                log.error("UID %s: forwarded but failed to remove label; will resend next run", uid)
                sent += 1

        log.warning("done: %d forwarded, %d failed%s", sent, failed, "" if args.send else " (dry run)")
        return 1 if failed else 0
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
"""IMAP poller fallback (inactive until credentials exist).

Activates automatically when IMAP_HOST and IMAP_PASSWORD are set in the
environment (e.g. a Google Workspace mailbox for vendas@). Polls the
configured folder, feeds unseen messages through the same pipeline as
the SMTP listener, then marks them seen.

imaplib is synchronous; the poll runs in a worker thread so the event
loop is never blocked.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
from email import policy

from .classify import headers_from_message
from .config import settings
from .pipeline import process_inbound
from .smtp_server import extract_text

log = logging.getLogger("cb-crm-inbound.imap")


def _fetch_unseen_sync() -> list[bytes]:
    conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        conn.login(settings.imap_user, settings.imap_password)
        conn.select(settings.imap_folder)
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []
        raw_messages: list[bytes] = []
        for num in data[0].split():
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status == "OK" and msg_data and msg_data[0] is not None:
                raw_messages.append(msg_data[0][1])
                conn.store(num, "+FLAGS", "\\Seen")
        return raw_messages
    finally:
        try:
            conn.logout()
        except imaplib.IMAP4.error:
            pass


async def poll_once() -> int:
    raw_messages = await asyncio.to_thread(_fetch_unseen_sync)
    for raw in raw_messages:
        msg = email.message_from_bytes(raw, policy=policy.default)
        from email.utils import parseaddr

        _, from_email = parseaddr(str(msg.get("From", "")))
        await process_inbound(
            from_email=from_email,
            subject=str(msg.get("Subject", "")),
            body_text=extract_text(msg),
            headers=headers_from_message(msg),
            message_id=(msg.get("Message-ID") or "").strip() or None,
            source="imap",
        )
    return len(raw_messages)


async def run_forever() -> None:
    log.info(
        "IMAP poller active: host=%s user=%s folder=%s every %ss",
        settings.imap_host, settings.imap_user, settings.imap_folder,
        settings.imap_poll_seconds,
    )
    while True:
        try:
            count = await poll_once()
            if count:
                log.info("IMAP poll processed %d message(s)", count)
        except (imaplib.IMAP4.error, OSError) as exc:
            log.error("IMAP poll failed: %s", exc)
        await asyncio.sleep(settings.imap_poll_seconds)

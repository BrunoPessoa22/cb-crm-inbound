"""SMTP listener for vendas@reply.culturabuilder.com.

MX for reply.culturabuilder.com points at this box; the container maps
host port 25 to SMTP_LISTEN_PORT. Mail for any mailbox at the inbound
domain is accepted and piped through the shared pipeline.

Failure semantics: if processing raises, we answer 451 (transient) so the
sending MTA retries later — replies are never silently lost while the DB
or Telegram is down.
"""
from __future__ import annotations

import logging
from email import policy
from email.parser import BytesParser

from aiosmtpd.smtp import SMTP, Envelope, Session

from .classify import headers_from_message
from .config import settings
from .pipeline import process_inbound

log = logging.getLogger("cb-crm-inbound.smtp")


def extract_text(msg) -> str:
    """Prefer text/plain; fall back to a crude de-tagging of text/html."""
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        try:
            return body.get_content()
        except (KeyError, LookupError, UnicodeDecodeError):
            pass
    body = msg.get_body(preferencelist=("html",))
    if body is not None:
        try:
            import re

            html = body.get_content()
            text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                          flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            return " ".join(text.split())
        except (KeyError, LookupError, UnicodeDecodeError):
            pass
    return ""


class InboundHandler:
    async def handle_RCPT(
        self,
        server: SMTP,
        session: Session,
        envelope: Envelope,
        address: str,
        rcpt_options: list,
    ) -> str:
        domain = address.rpartition("@")[2].lower()
        if domain != settings.inbound_domain.lower():
            return "550 relay denied"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(
        self, server: SMTP, session: Session, envelope: Envelope
    ) -> str:
        try:
            msg = BytesParser(policy=policy.default).parsebytes(
                envelope.content
            )
            headers = headers_from_message(msg)
            from_header = str(msg.get("From", envelope.mail_from or ""))
            # Bare address for contact matching.
            from email.utils import parseaddr

            _, from_email = parseaddr(from_header)
            if not from_email:
                from_email = envelope.mail_from or ""
            subject = str(msg.get("Subject", ""))
            message_id = (msg.get("Message-ID") or "").strip() or None
            text = extract_text(msg)
            summary = await process_inbound(
                from_email=from_email,
                subject=subject,
                body_text=text,
                headers=headers,
                message_id=message_id,
                source="smtp",
            )
            log.info("smtp processed: %s", summary)
            return "250 Message accepted for delivery"
        except Exception:
            # Deliberately broad: any processing failure must yield a
            # TRANSIENT SMTP error so the sender retries instead of the
            # reply being lost. The traceback is preserved in the log.
            log.exception("smtp processing failed; returning 451")
            return "451 Requested action aborted: local error in processing"

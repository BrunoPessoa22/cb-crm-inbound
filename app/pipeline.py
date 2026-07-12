"""Shared processing pipeline for all three inbound transports
(SMTP listener, Resend email.received webhook, IMAP poller)."""
from __future__ import annotations

import logging

from . import actions, notify
from .classify import Classification, classify
from .db import get_pool

log = logging.getLogger("cb-crm-inbound.pipeline")


async def process_inbound(
    from_email: str,
    subject: str,
    body_text: str,
    headers: dict[str, str],
    message_id: str | None,
    source: str,
) -> dict:
    """Classify one inbound email and run every CRM side effect.

    Returns a JSON-serializable summary (also used as the webhook
    response body, which makes log correlation trivial).
    """
    from_email = (from_email or "").strip().strip("<>").lower()
    preview = " ".join((body_text or "").split())[:500]
    cls: Classification = classify(from_email, subject, body_text, headers)
    log.info(
        "inbound source=%s from=%s subject=%r category=%s reason=%s msgid=%s",
        source, from_email, (subject or "")[:120], cls.category, cls.reason,
        message_id,
    )

    pool = await get_pool()
    summary: dict = {
        "source": source,
        "from": from_email,
        "category": cls.category,
        "reason": cls.reason,
        "message_id": message_id,
    }

    if cls.category == "reply":
        result = await actions.handle_real_reply(
            pool, from_email, subject, preview, message_id,
            full_text=body_text or "",
        )
        summary.update(
            contact_id=result.contact_id,
            duplicate=result.duplicate,
            enrollments_stopped=result.enrollments_stopped,
            notes=result.notes,
        )
        if result.duplicate:
            return summary
        if result.contact_id is not None:
            summary["telegram"] = await notify.telegram_lead_alert(
                from_email, result.contact_name, result.company_name,
                subject, preview,
            )
            summary["forwarded"] = await notify.forward_reply_copy(
                from_email, subject, body_text
            )
        else:
            # Unknown sender: keep Bruno's inbox in the loop but do not
            # touch the CRM and do not page Telegram (spam protection).
            summary["forwarded"] = await notify.forward_reply_copy(
                from_email, subject, body_text
            )
        return summary

    if cls.category == "optout":
        result = await actions.handle_optout(
            pool, from_email, subject, preview, message_id
        )
        summary.update(
            contact_id=result.contact_id,
            duplicate=result.duplicate,
            enrollments_stopped=result.enrollments_stopped,
            notes=result.notes,
        )
        # Polite by silence: nothing is ever sent back to an opt-out.
        return summary

    if cls.category == "ooo":
        result = await actions.handle_ooo(
            pool, from_email, subject, preview, message_id
        )
        summary.update(
            contact_id=result.contact_id,
            duplicate=result.duplicate,
            enrollments_paused=result.enrollments_paused,
            notes=result.notes,
        )
        return summary

    # bounce / auto: log only. DSNs for our outbound go through the
    # Resend events webhook, not here.
    summary["notes"] = ["logged only"]
    return summary

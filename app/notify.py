"""Telegram alerts and reply forwarding."""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("cb-crm-inbound.notify")


async def telegram_lead_alert(
    sender: str,
    contact_name: str | None,
    company: str | None,
    subject: str,
    preview: str,
) -> bool:
    """Instant [LEAD] alert to Bruno. Returns True when delivered."""
    if not settings.telegram_bot_token:
        log.warning("telegram token not configured; alert skipped")
        return False
    lines = [
        "[LEAD] Reply detectada no funil B2B",
        f"De: {contact_name or 'desconhecido'} <{sender}>",
    ]
    if company:
        lines.append(f"Empresa: {company}")
    lines.append(f"Assunto: {subject or '(sem assunto)'}")
    if preview:
        lines.append(f"Preview: {preview[:400]}")
    text = "\n".join(lines)[:4000]
    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                data={"chat_id": settings.telegram_chat_id, "text": text},
            )
        if resp.status_code != 200:
            log.error("telegram alert failed: %s %s", resp.status_code,
                      resp.text[:200])
            return False
        return True
    except httpx.HTTPError as exc:
        log.error("telegram alert error: %s", exc)
        return False


async def forward_reply_copy(
    original_from: str,
    subject: str,
    body_text: str,
) -> bool:
    """Send a copy of a real reply to Bruno's inbox via Resend.

    Reply-To is set to the prospect so Bruno can answer directly from
    his mail client.
    """
    payload = {
        "from": settings.forward_from,
        "to": [settings.forward_replies_to],
        "reply_to": original_from,
        "subject": f"Fwd: {subject or '(sem assunto)'}",
        "text": (
            f"Resposta recebida em vendas@{settings.inbound_domain}\n"
            f"De: {original_from}\n"
            f"Assunto: {subject}\n"
            f"{'-' * 40}\n\n"
            f"{body_text[:50000]}"
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
        if resp.status_code not in (200, 201):
            log.error("forward failed: %s %s", resp.status_code,
                      resp.text[:300])
            return False
        return True
    except httpx.HTTPError as exc:
        log.error("forward error: %s", exc)
        return False

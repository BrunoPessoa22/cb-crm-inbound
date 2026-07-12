"""Minimal Resend API client (received-email content fetch).

The email.received webhook carries only metadata; body and headers must
be fetched from GET /emails/receiving/{id}.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("cb-crm-inbound.resend")


async def get_received_email(email_id: str) -> dict | None:
    url = f"https://api.resend.com/emails/receiving/{email_id}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
        if resp.status_code != 200:
            log.error(
                "get_received_email %s failed: %s %s",
                email_id, resp.status_code, resp.text[:300],
            )
            return None
        return resp.json()
    except httpx.HTTPError as exc:
        log.error("get_received_email %s error: %s", email_id, exc)
        return None

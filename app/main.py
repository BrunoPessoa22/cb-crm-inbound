"""cb-crm-inbound — reply detection for the CB B2B pipeline.

Transports:
- SMTP listener (aiosmtpd protocol on the SAME event loop) for
  vendas@reply.culturabuilder.com — live path today.
- POST /webhooks/resend-inbound — Resend email.received (svix-verified);
  becomes the primary path once the Resend plan allows the custom
  receiving domain.
- POST /webhooks/resend-events — Resend outbound bounce/complaint events.
- IMAP poller — dormant until IMAP_HOST/IMAP_PASSWORD exist.

Single event loop, single asyncpg pool, shared classification pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from aiosmtpd.smtp import SMTP
from fastapi import FastAPI, Request, Response

from . import actions, imap_poller, resend_client
from .config import settings
from .db import close_pool, get_pool
from .pipeline import process_inbound
from .smtp_server import InboundHandler
from .svix_verify import SignatureError, verify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# httpx logs full request URLs at INFO; the Telegram URL embeds the bot
# token. Never let it reach container logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("cb-crm-inbound.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    handler = InboundHandler()
    smtp_server = await loop.create_server(
        lambda: SMTP(
            handler,
            data_size_limit=settings.smtp_max_message_bytes,
            ident="cb-crm-inbound",
        ),
        host=settings.smtp_listen_host,
        port=settings.smtp_listen_port,
    )
    log.info(
        "SMTP listener on %s:%d for domain %s",
        settings.smtp_listen_host, settings.smtp_listen_port,
        settings.inbound_domain,
    )
    imap_task: asyncio.Task | None = None
    if settings.imap_enabled:
        imap_task = asyncio.create_task(imap_poller.run_forever())
    else:
        log.info("IMAP poller dormant (IMAP_HOST/IMAP_PASSWORD not set)")
    try:
        yield
    finally:
        smtp_server.close()
        await smtp_server.wait_closed()
        if imap_task is not None:
            imap_task.cancel()
        await close_pool()


app = FastAPI(title="cb-crm-inbound", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        one = await conn.fetchval("SELECT 1")
    return {
        "ok": one == 1,
        "smtp_domain": settings.inbound_domain,
        "imap_enabled": settings.imap_enabled,
    }


def _verified_event(
    request: Request, payload: bytes, secret: str
) -> dict | None:
    try:
        verify(
            secret=secret,
            payload=payload,
            svix_id=request.headers.get("svix-id", ""),
            svix_timestamp=request.headers.get("svix-timestamp", ""),
            svix_signature=request.headers.get("svix-signature", ""),
        )
    except SignatureError as exc:
        log.warning("webhook signature rejected: %s", exc)
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        log.warning("webhook payload is not JSON")
        return None


@app.post("/webhooks/resend-inbound")
async def resend_inbound(request: Request) -> Response:
    payload = await request.body()
    event = _verified_event(request, payload, settings.resend_webhook_secret)
    if event is None:
        return Response(status_code=401)
    if event.get("type") != "email.received":
        return Response(
            content=json.dumps({"ignored": event.get("type")}),
            media_type="application/json",
        )

    data = event.get("data", {})
    email_id = data.get("email_id", "")
    from_email = data.get("from", "")
    subject = data.get("subject", "")

    # Webhook has metadata only; fetch body + headers from the API.
    body_text = ""
    headers: dict[str, str] = {}
    full = await resend_client.get_received_email(email_id) if email_id else None
    if full is not None:
        body_text = full.get("text") or ""
        if not body_text and full.get("html"):
            import re

            html = full["html"]
            stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                              flags=re.DOTALL | re.IGNORECASE)
            body_text = " ".join(re.sub(r"<[^>]+>", " ", stripped).split())
        raw_headers = full.get("headers") or {}
        headers = {str(k): str(v) for k, v in raw_headers.items()}
        message_id = full.get("message_id") or data.get("message_id")
    else:
        message_id = data.get("message_id")

    summary = await process_inbound(
        from_email=from_email,
        subject=subject,
        body_text=body_text,
        headers=headers,
        message_id=message_id,
        source="resend-webhook",
    )
    return Response(
        content=json.dumps(summary, default=str),
        media_type="application/json",
    )


@app.post("/webhooks/resend-events")
async def resend_events(request: Request) -> Response:
    payload = await request.body()
    event = _verified_event(
        request, payload, settings.resend_events_webhook_secret
    )
    if event is None:
        return Response(status_code=401)

    event_type = event.get("type", "")
    data = event.get("data", {})
    to_list = data.get("to") or []
    to_email = to_list[0] if to_list else ""
    summary: dict = {"type": event_type, "to": to_email}

    pool = await get_pool()
    if event_type == "email.bounced" and to_email:
        bounce = data.get("bounce") or {}
        hard = str(bounce.get("type", "")).lower() == "permanent"
        result = await actions.handle_bounce(
            pool, to_email, hard,
            {"bounce_type": bounce.get("type"),
             "sub_type": bounce.get("subType"),
             "message": str(bounce.get("message", ""))[:500],
             "email_id": data.get("email_id")},
        )
        summary.update(
            hard=hard,
            contact_id=result.contact_id,
            enrollments_stopped=result.enrollments_stopped,
            notes=result.notes,
        )
    elif event_type == "email.complained" and to_email:
        result = await actions.handle_optout(
            pool, to_email,
            subject=data.get("subject", ""),
            preview="(spam complaint)",
            message_id=data.get("email_id"),
            reason="complaint",
        )
        summary.update(
            contact_id=result.contact_id,
            enrollments_stopped=result.enrollments_stopped,
            notes=result.notes,
        )
    else:
        summary["ignored"] = True

    log.info("resend-events processed: %s", summary)
    return Response(
        content=json.dumps(summary, default=str),
        media_type="application/json",
    )

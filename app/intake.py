"""Landing intake: lead forms and visit beacons from culturabuilder-landings.

Since 13 Oct 2026 the CRM lives in aguia-crm-db on the Aguia box, which
Vercel cannot reach (Postgres is never exposed to the internet). The
landings POST here instead of writing SQL; the writes are exactly the ones
they used to make through the landings_writer role.

- Auth: `Authorization: Bearer <LANDINGS_INTAKE_SECRET>`. An empty secret
  disables the intake (503, fail closed). Auth runs before body validation,
  so unauthenticated callers learn nothing about the schema.
- Rate limits: in-process fixed windows (single uvicorn worker), per
  visitor IP (`X-Visitor-IP`, trusted only after auth) and global per route.
- Test mode: `X-Intake-Test: 1` runs the full write path and rolls it back.
  crm_events is append-only (trigger), so a committed probe could never be
  cleaned up; a rolled-back one leaves nothing behind.
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import settings
from .db import get_pool

log = logging.getLogger("cb-crm-inbound.intake")

_SLUG = r"^[a-z0-9][a-z0-9-]{0,39}$"
_TOKEN = r"^[A-Za-z0-9_-]{8,80}$"
_PHONE = r"^[+\d\s().-]{8,40}$"
_EMAIL = r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$"

# (per-visitor, global) requests per window. The landings keep their own
# per-instance limits; these hold across Vercel instances.
WINDOW_SECONDS = 600.0
LIMITS: dict[str, tuple[int, int]] = {"lead": (5, 200), "visit": (60, 3000)}


class LeadIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    email: str = Field(max_length=200, pattern=_EMAIL)
    company: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, pattern=_PHONE)
    vertical: str = Field(pattern=_SLUG)
    token: str | None = Field(default=None, pattern=_TOKEN)

    @field_validator("phone", "token", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class VisitIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: str = Field(pattern=_TOKEN)
    vertical: str = Field(pattern=_SLUG)


class RateLimiter:
    """Fixed-window counters kept in process memory."""

    def __init__(self, max_keys: int = 5000) -> None:
        self._buckets: dict[str, tuple[int, float]] = {}
        self._max_keys = max_keys

    def allow(
        self, key: str, limit: int, window: float, now: float | None = None
    ) -> bool:
        now = time.monotonic() if now is None else now
        if len(self._buckets) > self._max_keys:
            self._buckets = {
                k: b for k, b in self._buckets.items() if b[1] > now
            }
        count, reset_at = self._buckets.get(key, (0, 0.0))
        if reset_at <= now:
            count, reset_at = 0, now + window
        if count >= limit:
            return False
        self._buckets[key] = (count + 1, reset_at)
        return True


limiter = RateLimiter()


class _TestRollback(Exception):
    """Raised inside the transaction to undo a test-mode write."""


def require_secret(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    secret = settings.landings_intake_secret
    if not secret:
        raise HTTPException(status_code=503, detail="intake disabled")
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def _limit(route: str, visitor_ip: str | None) -> None:
    per_visitor, overall = LIMITS[route]
    if visitor_ip and not limiter.allow(
        f"{route}:{visitor_ip[:64]}", per_visitor, WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="rate_limited")
    if not limiter.allow(f"{route}:*", overall, WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail="rate_limited")


def limit_lead(
    x_visitor_ip: Annotated[str | None, Header()] = None,
) -> None:
    _limit("lead", x_visitor_ip)


def limit_visit(
    x_visitor_ip: Annotated[str | None, Header()] = None,
) -> None:
    _limit("visit", x_visitor_ip)


async def write_lead(conn: asyncpg.Connection, lead: LeadIn) -> tuple[int, bool]:
    """Company + contact upsert, form_submit touch and event. Returns
    (contact_id, contact_created)."""
    source = f"landing-{lead.vertical}"
    email = lead.email.lower()
    company_id = await conn.fetchval(
        "SELECT id FROM public.crm_companies WHERE lower(name) = lower($1) LIMIT 1",
        lead.company,
    )
    if company_id is None:
        company_id = await conn.fetchval(
            "INSERT INTO public.crm_companies (name, source) VALUES ($1, $2) RETURNING id",
            lead.company,
            source,
        )
    contact_id = await conn.fetchval(
        "SELECT id FROM public.crm_contacts WHERE lower(email) = $1 LIMIT 1",
        email,
    )
    created = contact_id is None
    if created:
        contact_id = await conn.fetchval(
            """
            INSERT INTO public.crm_contacts
                (company_id, name, role, email, phone, source, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'new')
            RETURNING id
            """,
            company_id, lead.name, lead.role, email, lead.phone, source,
        )
    else:
        await conn.execute(
            """
            UPDATE public.crm_contacts
               SET company_id = COALESCE(company_id, $2),
                   role       = COALESCE(role, $3),
                   phone      = COALESCE(phone, $4)
             WHERE id = $1
            """,
            contact_id, company_id, lead.role, lead.phone,
        )
    await conn.execute(
        """
        INSERT INTO public.crm_touches
            (contact_id, channel, direction, kind, subject, body_preview)
        VALUES ($1, 'event', 'in', 'form_submit', $2, $3)
        """,
        contact_id, source, f"{lead.name} · {lead.role} · {lead.company}",
    )
    await conn.execute(
        """
        INSERT INTO public.crm_events (event_type, contact_id, meta)
        VALUES ('form_submit', $1, $2::jsonb)
        """,
        contact_id,
        json.dumps(
            {"vertical": lead.vertical, "source": source,
             "company": lead.company, "visit_token": lead.token},
            ensure_ascii=False,
        ),
    )
    return contact_id, created


async def write_visit(conn: asyncpg.Connection, visit: VisitIn) -> int | None:
    """page_visit touch + event for the token's contact, at most one per
    contact per 24h. Returns the contact id when a visit was recorded."""
    contact_id = await conn.fetchval(
        "SELECT id FROM public.crm_contacts WHERE visit_token = $1 LIMIT 1",
        visit.token,
    )
    if contact_id is None:
        return None
    recent = await conn.fetchval(
        """
        SELECT 1 FROM public.crm_touches
         WHERE contact_id = $1 AND kind = 'page_visit'
           AND occurred_at >= now() - interval '24 hours'
         LIMIT 1
        """,
        contact_id,
    )
    if recent:
        return None
    await conn.execute(
        """
        INSERT INTO public.crm_touches (contact_id, channel, direction, kind, subject)
        VALUES ($1, 'other', 'in', 'page_visit', $2)
        """,
        contact_id, f"landing-{visit.vertical}",
    )
    await conn.execute(
        """
        INSERT INTO public.crm_events (event_type, contact_id, meta)
        VALUES ('page_visit', $1, $2::jsonb)
        """,
        contact_id, json.dumps({"vertical": visit.vertical}),
    )
    return contact_id


router = APIRouter(prefix="/api")


@router.post("/leads", dependencies=[Depends(require_secret), Depends(limit_lead)])
async def intake_lead(
    lead: LeadIn,
    x_intake_test: Annotated[str | None, Header()] = None,
) -> dict:
    test = x_intake_test == "1"
    pool = await get_pool()
    outcome: tuple[int, bool] | None = None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                outcome = await write_lead(conn, lead)
                if test:
                    raise _TestRollback
    except _TestRollback:
        pass
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
        log.error("lead write failed vertical=%s: %r", lead.vertical, exc)
        raise HTTPException(status_code=500, detail="internal") from exc
    assert outcome is not None
    contact_id, created = outcome
    log.info(
        "lead %s contact_id=%s created=%s vertical=%s",
        "rolled back (test)" if test else "stored",
        contact_id, created, lead.vertical,
    )
    return {"ok": True, "contact_id": contact_id, "created": created,
            "test": test, "rolled_back": test}


@router.post("/visits", dependencies=[Depends(require_secret), Depends(limit_visit)])
async def intake_visit(
    visit: VisitIn,
    x_intake_test: Annotated[str | None, Header()] = None,
) -> dict:
    test = x_intake_test == "1"
    pool = await get_pool()
    recorded: int | None = None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                recorded = await write_visit(conn, visit)
                if test:
                    raise _TestRollback
    except _TestRollback:
        pass
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
        log.error("visit write failed vertical=%s: %r", visit.vertical, exc)
        raise HTTPException(status_code=500, detail="internal") from exc
    if recorded is not None:
        log.info(
            "visit %s contact_id=%s vertical=%s",
            "rolled back (test)" if test else "recorded", recorded, visit.vertical,
        )
    return {"ok": True, "recorded": recorded is not None,
            "test": test, "rolled_back": test}

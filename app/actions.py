"""CRM side effects for classified inbound email.

All SQL is schema-qualified and idempotent. Dedupe key is the RFC
Message-ID stored in public.crm_touches.external_id (unique partial
index), so webhook retries and SMTP redeliveries never double-fire.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import asyncpg

log = logging.getLogger("cb-crm-inbound.actions")


@dataclass
class ActionResult:
    category: str
    duplicate: bool = False
    contact_id: int | None = None
    contact_name: str | None = None
    company_name: str | None = None
    enrollments_stopped: int = 0
    enrollments_paused: int = 0
    notes: list[str] = field(default_factory=list)


async def _find_contact(
    conn: asyncpg.Connection, email: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT ct.id, ct.name, ct.status, co.name AS company_name
        FROM public.crm_contacts ct
        LEFT JOIN public.crm_companies co ON co.id = ct.company_id
        WHERE lower(ct.email) = lower($1)
        """,
        email,
    )


async def _record_touch(
    conn: asyncpg.Connection,
    contact_id: int,
    kind: str,
    subject: str,
    preview: str,
    message_id: str | None,
) -> bool:
    """Insert the inbound touch. Returns False when this message_id was
    already processed (duplicate delivery)."""
    row = await conn.fetchrow(
        """
        INSERT INTO public.crm_touches
            (contact_id, channel, direction, kind, subject, body_preview,
             external_id)
        VALUES ($1, 'email', 'in', $2, $3, $4, $5)
        ON CONFLICT (external_id) WHERE external_id IS NOT NULL DO NOTHING
        RETURNING id
        """,
        contact_id,
        kind,
        subject[:500] if subject else None,
        preview[:500] if preview else None,
        message_id,
    )
    return row is not None


async def _event(
    conn: asyncpg.Connection,
    event_type: str,
    contact_id: int | None,
    meta: dict,
) -> None:
    await conn.execute(
        """
        INSERT INTO public.crm_events (event_type, contact_id, meta)
        VALUES ($1::public.crm_event_type, $2, $3::jsonb)
        """,
        event_type,
        contact_id,
        json.dumps(meta, ensure_ascii=False)[:8000],
    )


async def handle_real_reply(
    pool: asyncpg.Pool,
    from_email: str,
    subject: str,
    preview: str,
    message_id: str | None,
) -> ActionResult:
    result = ActionResult(category="reply")
    async with pool.acquire() as conn:
        async with conn.transaction():
            contact = await _find_contact(conn, from_email)
            if contact is None:
                result.notes.append("sender not in crm_contacts")
                return result
            result.contact_id = contact["id"]
            result.contact_name = contact["name"]
            result.company_name = contact["company_name"]

            inserted = await _record_touch(
                conn, contact["id"], "reply", subject, preview, message_id
            )
            if not inserted:
                result.duplicate = True
                return result

            # Never downgrade a further-along status.
            await conn.execute(
                """
                UPDATE public.crm_contacts
                SET status = 'replied'
                WHERE id = $1 AND status IN ('new', 'enrolled')
                """,
                contact["id"],
            )
            stopped = await conn.execute(
                """
                UPDATE public.crm_enrollments
                SET status = 'stopped_reply'
                WHERE contact_id = $1 AND status IN ('active', 'paused')
                """,
                contact["id"],
            )
            result.enrollments_stopped = int(stopped.split()[-1])
            await _event(
                conn,
                "replied",
                contact["id"],
                {
                    "subject": subject,
                    "preview": preview[:300],
                    "message_id": message_id,
                    "source": "cb-crm-inbound",
                },
            )
    return result


async def handle_optout(
    pool: asyncpg.Pool,
    from_email: str,
    subject: str,
    preview: str,
    message_id: str | None,
    reason: str = "optout_reply",
) -> ActionResult:
    result = ActionResult(category="optout")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO public.crm_suppressions (email, reason, source)
                VALUES ($1, $2, 'cb-crm-inbound')
                ON CONFLICT (lower(email)) WHERE email IS NOT NULL DO NOTHING
                """,
                from_email.lower(),
                reason,
            )
            contact = await _find_contact(conn, from_email)
            if contact is None:
                result.notes.append("sender not in crm_contacts; suppressed only")
                await _event(
                    conn,
                    "opt_out",
                    None,
                    {"email": from_email.lower(), "subject": subject,
                     "message_id": message_id, "source": "cb-crm-inbound"},
                )
                return result
            result.contact_id = contact["id"]
            result.contact_name = contact["name"]
            result.company_name = contact["company_name"]

            inserted = await _record_touch(
                conn, contact["id"], "optout", subject, preview, message_id
            )
            if not inserted:
                result.duplicate = True
                return result

            await conn.execute(
                "UPDATE public.crm_contacts SET status = 'opted_out' WHERE id = $1",
                contact["id"],
            )
            stopped = await conn.execute(
                """
                UPDATE public.crm_enrollments
                SET status = 'stopped_optout'
                WHERE contact_id = $1 AND status IN ('active', 'paused')
                """,
                contact["id"],
            )
            result.enrollments_stopped = int(stopped.split()[-1])
            await _event(
                conn,
                "opt_out",
                contact["id"],
                {"subject": subject, "message_id": message_id,
                 "source": "cb-crm-inbound"},
            )
    return result


async def handle_ooo(
    pool: asyncpg.Pool,
    from_email: str,
    subject: str,
    preview: str,
    message_id: str | None,
) -> ActionResult:
    result = ActionResult(category="ooo")
    async with pool.acquire() as conn:
        async with conn.transaction():
            contact = await _find_contact(conn, from_email)
            if contact is None:
                result.notes.append("sender not in crm_contacts")
                return result
            result.contact_id = contact["id"]
            result.contact_name = contact["name"]
            result.company_name = contact["company_name"]

            inserted = await _record_touch(
                conn, contact["id"], "auto_reply_ooo", subject, preview,
                message_id,
            )
            if not inserted:
                result.duplicate = True
                return result

            paused = await conn.execute(
                """
                UPDATE public.crm_enrollments
                SET status = 'paused',
                    next_action_at = now() + interval '7 days'
                WHERE contact_id = $1 AND status = 'active'
                """,
                contact["id"],
            )
            result.enrollments_paused = int(paused.split()[-1])
    return result


async def handle_bounce(
    pool: asyncpg.Pool,
    to_email: str,
    hard: bool,
    detail: dict,
) -> ActionResult:
    """Bounce/complaint from the Resend outbound events webhook.

    ``to_email`` is the address WE sent to (the bounced recipient).
    """
    result = ActionResult(category="bounce")
    async with pool.acquire() as conn:
        async with conn.transaction():
            if hard:
                await conn.execute(
                    """
                    INSERT INTO public.crm_suppressions (email, reason, source)
                    VALUES ($1, 'hard_bounce', 'cb-crm-inbound')
                    ON CONFLICT (lower(email)) WHERE email IS NOT NULL
                    DO NOTHING
                    """,
                    to_email.lower(),
                )
            contact = await _find_contact(conn, to_email)
            if contact is None:
                result.notes.append("recipient not in crm_contacts")
                await _event(
                    conn, "bounce", None,
                    {"email": to_email.lower(), "hard": hard,
                     "source": "cb-crm-inbound", **detail},
                )
                return result
            result.contact_id = contact["id"]
            result.contact_name = contact["name"]
            result.company_name = contact["company_name"]

            await conn.execute(
                """
                UPDATE public.crm_contacts
                SET status = 'bounced'
                WHERE id = $1 AND status IN ('new', 'enrolled')
                """,
                contact["id"],
            )
            stopped = await conn.execute(
                """
                UPDATE public.crm_enrollments
                SET status = 'stopped_bounce'
                WHERE contact_id = $1 AND status IN ('active', 'paused')
                """,
                contact["id"],
            )
            result.enrollments_stopped = int(stopped.split()[-1])
            await _event(
                conn, "bounce", contact["id"],
                {"hard": hard, "source": "cb-crm-inbound", **detail},
            )
    return result

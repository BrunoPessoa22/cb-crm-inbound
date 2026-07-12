"""asyncpg pool for the CB Neon CRM database.

Hard-won lesson from cb-crm: the Neon endpoint is POOLED and shared with
the CB production app — sessions can arrive with an EMPTY search_path.
Every statement is schema-qualified (public.crm_...) AND the pool pins
search_path=public via server_settings.
"""
from __future__ import annotations

import asyncpg

from .config import settings

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.crm_database_url,
            min_size=0,
            max_size=4,
            command_timeout=30,
            server_settings={"search_path": "public"},
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None

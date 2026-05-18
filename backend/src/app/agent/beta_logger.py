"""
PostgreSQL-backed session conversion tracker for the WooAgent beta.

Records per-session:
  - session_id, store_id, language
  - turn count, tool call count
  - LLM route distribution (groq/gemini/gpt-mini/gpt-4o)
  - cart value at checkout
  - checkout_reached flag
  - session start/end timestamps

No-ops silently when BETA_LOGGING_ENABLED=false or DATABASE_URL is missing,
so this is completely opt-in and never breaks production.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("BETA_LOGGING_ENABLED", "false").lower() == "true"
_DB_URL  = os.getenv("DATABASE_URL", "")

# ── Schema DDL (run once on startup) ─────────────────────────────────────────

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS beta_sessions (
    id               SERIAL PRIMARY KEY,
    session_id       TEXT NOT NULL UNIQUE,
    store_id         TEXT,
    language         TEXT,
    turns            INT     DEFAULT 0,
    tool_calls       INT     DEFAULT 0,
    route_groq       INT     DEFAULT 0,
    route_gemini     INT     DEFAULT 0,
    route_gpt_mini   INT     DEFAULT 0,
    route_gpt4o      INT     DEFAULT 0,
    cart_value       NUMERIC DEFAULT 0,
    checkout_reached BOOLEAN DEFAULT FALSE,
    started_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);
"""

_UPSERT = """
INSERT INTO beta_sessions (
    session_id, store_id, language,
    turns, tool_calls,
    route_groq, route_gemini, route_gpt_mini, route_gpt4o,
    cart_value, checkout_reached, started_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)
ON CONFLICT (session_id) DO UPDATE SET
    language         = EXCLUDED.language,
    turns            = beta_sessions.turns            + EXCLUDED.turns,
    tool_calls       = beta_sessions.tool_calls       + EXCLUDED.tool_calls,
    route_groq       = beta_sessions.route_groq       + EXCLUDED.route_groq,
    route_gemini     = beta_sessions.route_gemini     + EXCLUDED.route_gemini,
    route_gpt_mini   = beta_sessions.route_gpt_mini   + EXCLUDED.route_gpt_mini,
    route_gpt4o      = beta_sessions.route_gpt4o      + EXCLUDED.route_gpt4o,
    cart_value       = GREATEST(beta_sessions.cart_value, EXCLUDED.cart_value),
    checkout_reached = beta_sessions.checkout_reached OR EXCLUDED.checkout_reached,
    updated_at       = EXCLUDED.updated_at;
"""


class BetaLogger:
    """
    Fire-and-forget session telemetry logger.

    Usage:
        log = BetaLogger()
        await log.start()          # create table on startup (idempotent)

        await log.record_turn(
            session_id="abc123",
            store_id="store_1",
            language="ml",
            llm_route="gemini",
            tool_call_count=1,
            cart_value=0.0,
            checkout_reached=False,
        )

        await log.close()          # on shutdown
    """

    def __init__(self):
        self._pool = None
        self._enabled = _ENABLED and bool(_DB_URL)
        if _ENABLED and not _DB_URL:
            logger.warning("BETA_LOGGING_ENABLED=true but DATABASE_URL is not set — logging disabled")
        elif self._enabled:
            logger.info("BetaLogger: PostgreSQL session logging enabled")

    async def start(self) -> None:
        """Create connection pool and ensure table exists."""
        if not self._enabled:
            return
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(
                dsn=_DB_URL,
                min_size=1,
                max_size=3,
                command_timeout=5,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(_CREATE_TABLE)
            logger.info("BetaLogger: table ready")
        except Exception as e:
            logger.warning("BetaLogger: init failed (%s) — logging disabled", e)
            self._enabled = False
            self._pool = None

    async def record_turn(
        self,
        session_id: str,
        store_id: str = "",
        language: str = "en",
        llm_route: str = "gpt-4o-mini",
        tool_call_count: int = 0,
        cart_value: float = 0.0,
        checkout_reached: bool = False,
    ) -> None:
        """
        Upsert one turn's data into the session row.
        Silently no-ops on any error — never raises.
        """
        if not self._enabled or self._pool is None:
            return
        try:
            route_groq     = 1 if llm_route == "groq"       else 0
            route_gemini   = 1 if llm_route == "gemini"     else 0
            route_gpt_mini = 1 if llm_route == "gpt-4o-mini" else 0
            route_gpt4o    = 1 if llm_route == "gpt-4o"     else 0
            now = datetime.now(timezone.utc)

            async with self._pool.acquire() as conn:
                await conn.execute(
                    _UPSERT,
                    session_id,
                    store_id,
                    language,
                    1,                  # turns += 1
                    tool_call_count,
                    route_groq,
                    route_gemini,
                    route_gpt_mini,
                    route_gpt4o,
                    cart_value,
                    checkout_reached,
                    now,                # started_at (ignored on conflict)
                    now,                # updated_at
                )
        except Exception as e:
            logger.debug("BetaLogger: record_turn failed (%s)", e)

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:
                pass


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[BetaLogger] = None


def get_beta_logger() -> BetaLogger:
    global _instance
    if _instance is None:
        _instance = BetaLogger()
    return _instance

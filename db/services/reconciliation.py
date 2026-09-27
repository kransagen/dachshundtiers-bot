"""Phase E, E3 — periodic reconciliation service.

Reconciliation drains confirmed outbox wedges and mirrors the guild's
current Discord roles into PostgreSQL. It is STRICTLY observe-only on
Discord: it never adds/removes/edits roles and never writes a tier for
ambiguous input or unknown players (anomalies, never fabrications).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.services.mirror_sync import DiscordSyncOutcome, DiscordSyncService
from db.services.outbox_consumer import (
    OutboxConsumption,
    OutboxConsumer,
    default_stale_cutoff,
)

RECONCILE_COMMAND = "reconcile"
RECONCILE_MODE = "automatic"


@dataclass(frozen=True)
class ReconciliationOutcome:
    outbox_consumed: tuple[OutboxConsumption, ...]
    sync: DiscordSyncOutcome


class ReconciliationService:
    """Startup / hourly / on_member_update entry point. Never mutates Discord."""

    def __init__(
        self,
        *,
        sync: Optional[DiscordSyncService] = None,
        consumer: Optional[OutboxConsumer] = None,
    ) -> None:
        self._sync = sync or DiscordSyncService()
        self._consumer = consumer or OutboxConsumer()

    async def reconcile(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        members,
        triggered_by: Optional[int] = None,
        triggered_by_name: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> ReconciliationOutcome:
        # C1 audit fix: this is the ONLY production entry point that drives
        # the outbox consumer (bot.py calls reconcile() at startup and
        # hourly). Without in_progress_before, claim_next() only ever
        # selects status='pending' rows — a crash between claiming an event
        # and completing it (mark_done/mark_failed) left that event stuck at
        # 'in_progress' FOREVER, since no future pass would ever look at it
        # again. This was fully implemented and unit-tested
        # (OutboxConsumer.consume_one(in_progress_before=...)) but never
        # reached from here — the exact "recover PostgreSQL from confirmed
        # Discord state" guarantee the outbox exists for was silently dead
        # in the running bot. See db/repositories/outbox.py claim_next() for
        # how staleness is now measured from claimed_at, not created_at.
        consumed = await self._consumer.consume_many(
            session_factory, in_progress_before=default_stale_cutoff()
        )
        sync_outcome = await self._sync.sync_guild(
            session_factory,
            members=members,
            observed_at=observed_at,
            triggered_by=triggered_by,
            triggered_by_name=triggered_by_name,
            command=RECONCILE_COMMAND,
            mode=RECONCILE_MODE,
        )
        return ReconciliationOutcome(
            outbox_consumed=tuple(consumed), sync=sync_outcome
        )

    async def observe_member(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        member,
        triggered_by: Optional[int] = None,
        triggered_by_name: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> DiscordSyncOutcome:
        return await self._sync.sync_guild(
            session_factory,
            members=[member],
            observed_at=observed_at,
            triggered_by=triggered_by,
            triggered_by_name=triggered_by_name,
            command=RECONCILE_COMMAND,
            mode=RECONCILE_MODE,
        )
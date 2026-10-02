"""Retention of append-only operational tables.

Deletes old ``sync_actions``, finished ``outbox_events`` and ``audit_logs`` so
the hourly reconciliation cannot grow them without bound. Dead-letter and
failed outbox events are never purged — they need manual review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import AuditLog, OutboxEvent, SyncAction
from db.repositories.outbox import OUTBOX_DONE
from db.services.session import transaction

SYNC_ACTIONS_RETENTION = timedelta(days=90)
AUDIT_LOG_RETENTION = timedelta(days=365)
OUTBOX_DONE_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class RetentionOutcome:
    sync_actions: int
    audit_logs: int
    outbox_events: int


async def purge_old_records(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: Optional[datetime] = None,
    sync_actions_after: timedelta = SYNC_ACTIONS_RETENTION,
    audit_logs_after: timedelta = AUDIT_LOG_RETENTION,
    outbox_done_after: timedelta = OUTBOX_DONE_RETENTION,
) -> RetentionOutcome:
    now = now or datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        sync_actions = await session.execute(
            delete(SyncAction).where(SyncAction.created_at < now - sync_actions_after)
        )
        audit_logs = await session.execute(
            delete(AuditLog).where(AuditLog.created_at < now - audit_logs_after)
        )
        outbox_events = await session.execute(
            delete(OutboxEvent).where(
                OutboxEvent.status == OUTBOX_DONE,
                OutboxEvent.processed_at < now - outbox_done_after,
            )
        )
    return RetentionOutcome(
        sync_actions=sync_actions.rowcount or 0,
        audit_logs=audit_logs.rowcount or 0,
        outbox_events=outbox_events.rowcount or 0,
    )

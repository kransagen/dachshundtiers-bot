"""Outbox repository — the recoverability wedge for Discord-confirmed events.

Used when Discord succeeds but the DB write fails (invariant 7): the event is
enqueued on a SEPARATE session so it survives the failed main transaction, then
a reconciler replays it to bring PostgreSQL into alignment with Discord.
Retry policy: ``attempts`` is bumped when an event is claimed (so a replay
that crashes the process still counts). A failed attempt returns the event to
``pending`` (last_error recorded) after an exponential backoff
(``next_attempt_at``) until ``OUTBOX_MAX_ATTEMPTS``, then it becomes
``dead_letter`` for manual review. ``discord_role_confirmed`` flags that the Discord side already
happened — replaying such an event must never re-apply Discord mutations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import OutboxEvent

OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_RETRY_BASE_SECONDS = 60
OUTBOX_RETRY_MAX_SECONDS = 60 * 60

OUTBOX_PENDING = "pending"
OUTBOX_IN_PROGRESS = "in_progress"
OUTBOX_DONE = "done"
OUTBOX_FAILED = "failed"
OUTBOX_DEAD_LETTER = "dead_letter"


def retry_delay(attempts: int) -> timedelta:
    """Backoff before the next replay after ``attempts`` failed attempts."""
    seconds = OUTBOX_RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0)
    return timedelta(seconds=min(seconds, OUTBOX_RETRY_MAX_SECONDS))


class OutboxRepository:
    """Leaderless claim loop: claim_next -> process -> mark_done/mark_failed."""

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict,
        discord_role_confirmed: bool = False,
    ) -> OutboxEvent:
        row = OutboxEvent(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
            status=OUTBOX_PENDING,
            discord_role_confirmed=discord_role_confirmed,
        )
        session.add(row)
        await session.flush()
        return row

    async def claim_next(
        self,
        session: AsyncSession,
        *,
        event_type: Optional[str] = None,
        in_progress_before: Optional[datetime] = None,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
    ) -> Optional[OutboxEvent]:
        """Atomically claim the oldest due pending event (FOR UPDATE SKIP LOCKED).

        Events still inside their retry backoff (``next_attempt_at`` in the
        future) are skipped. Claiming bumps ``attempts``; a stale
        ``in_progress`` event that already used up ``max_attempts`` (it keeps
        crashing its consumer) is dead-lettered instead of reclaimed.

        ``in_progress_before`` — if given, stale ``in_progress`` events whose
        *claim* (not creation — see ``claimed_at``, M7 audit fix) predates the
        threshold are reclaimed (crash recovery). A row that was merely
        backlogged as ``pending`` for a long time before being claimed is NOT
        stale the instant it's claimed — only a claim that has itself been
        held past the threshold (i.e. the consumer that claimed it likely
        crashed) is eligible for reclaim.
        """
        now = datetime.now(timezone.utc)
        stmt = select(OutboxEvent)
        if in_progress_before is not None:
            stmt = stmt.where(
                (OutboxEvent.status == OUTBOX_PENDING)
                | (
                    (OutboxEvent.status == OUTBOX_IN_PROGRESS)
                    & (OutboxEvent.claimed_at.is_not(None))
                    & (OutboxEvent.claimed_at < in_progress_before)
                )
            )
        else:
            stmt = stmt.where(OutboxEvent.status == OUTBOX_PENDING)
        stmt = stmt.where(
            OutboxEvent.next_attempt_at.is_(None) | (OutboxEvent.next_attempt_at <= now)
        )
        if event_type is not None:
            stmt = stmt.where(OutboxEvent.event_type == event_type)
        stmt = (
            stmt.order_by(OutboxEvent.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        while True:
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            if row.status == OUTBOX_IN_PROGRESS and (row.attempts or 0) >= max_attempts:
                row.status = OUTBOX_DEAD_LETTER
                row.last_error = (
                    "claimed repeatedly without completing (consumer crash loop)"
                )
                await session.flush()
                continue
            row.status = OUTBOX_IN_PROGRESS
            row.claimed_at = now
            row.next_attempt_at = None
            row.attempts = (row.attempts or 0) + 1
            await session.flush()
            return row

    async def mark_done(
        self,
        session: AsyncSession,
        *,
        event_id: int,
        processed_at: Optional[datetime] = None,
        claimed_at: Optional[datetime] = None,
    ) -> bool:
        """Mark done; with ``claimed_at`` only if that claim is still the holder."""
        stmt = update(OutboxEvent).where(OutboxEvent.id == event_id)
        if claimed_at is not None:
            stmt = stmt.where(
                OutboxEvent.status == OUTBOX_IN_PROGRESS,
                OutboxEvent.claimed_at == claimed_at,
            )
        result = await session.execute(
            stmt.values(
                status=OUTBOX_DONE,
                processed_at=processed_at or datetime.now(timezone.utc),
            )
        )
        return (result.rowcount or 0) > 0

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        event_id: int,
        error: str,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
        claimed_at: Optional[datetime] = None,
    ) -> str:
        """Register a failure; returns the event's next status.

        ``attempts`` was already bumped by ``claim_next``. With ``claimed_at``
        a consumer that lost its claim (reclaimed as stale) changes nothing
        and gets ``""``.
        """
        stmt = select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
        if claimed_at is not None:
            stmt = stmt.where(
                OutboxEvent.status == OUTBOX_IN_PROGRESS,
                OutboxEvent.claimed_at == claimed_at,
            )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return ""
        row.last_error = (error or "")[:2000]
        if (row.attempts or 0) >= max_attempts:
            row.status = OUTBOX_DEAD_LETTER
        else:
            row.status = OUTBOX_PENDING
            row.claimed_at = None
            row.next_attempt_at = datetime.now(timezone.utc) + retry_delay(row.attempts or 1)
        await session.flush()
        return row.status

    async def list_by_status(
        self, session: AsyncSession, *, status: str, limit: int = 100
    ) -> list[OutboxEvent]:
        result = await session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.status == status)
            .order_by(OutboxEvent.created_at)
            .limit(limit)
        )
        return list(result.scalars())
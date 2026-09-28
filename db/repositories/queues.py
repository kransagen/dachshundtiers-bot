"""Queue + queue-entry repositories (HT3 challenge queues)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Queue, QueueEntry, QueueTester

QUEUE_ENTRY_WAITING = "waiting"
QUEUE_ENTRY_PULLED = "pulled"
QUEUE_ENTRY_LEFT = "left"
QUEUE_ENTRY_TESTED = "tested"


class QueueRepository:
    """One active (unclosed) queue per kit at a time."""

    async def open(
        self,
        session: AsyncSession,
        *,
        kit_id: int,
        name: str,
        started_at: Optional[datetime] = None,
        panel_channel_id: Optional[int] = None,
        panel_message_id: Optional[int] = None,
    ) -> Queue:
        row = Queue(
            kit_id=kit_id,
            name=name,
            started_at=started_at or datetime.now(timezone.utc),
            panel_channel_id=panel_channel_id,
            panel_message_id=panel_message_id,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_by_id(self, session: AsyncSession, queue_id: int) -> Optional[Queue]:
        return await session.get(Queue, queue_id)

    async def get_active(self, session: AsyncSession, *, kit_id: int) -> Optional[Queue]:
        result = await session.execute(
            select(Queue)
            .where(Queue.kit_id == kit_id, Queue.closed_at.is_(None))
            .order_by(Queue.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_active(
        self, session: AsyncSession
    ) -> list[Queue]:
        result = await session.execute(
            select(Queue)
            .where(Queue.closed_at.is_(None))
            .order_by(Queue.started_at)
        )
        return list(result.scalars())

    async def close(
        self, session: AsyncSession, *, queue_id: int, closed_at: Optional[datetime] = None
    ) -> Optional[Queue]:
        row = await self.get_by_id(session, queue_id)
        if row is None:
            return None
        row.closed_at = closed_at or datetime.now(timezone.utc)
        await session.flush()
        return row


class QueueEntryRepository:
    """Positioned queue entries; a player waits at most once per active queue."""

    async def enqueue(
        self,
        session: AsyncSession,
        *,
        queue_id: int,
        player_id: int,
        ign: str,
        kit_id: int,
        position: Optional[int] = None,
        joined_at: Optional[datetime] = None,
        username: Optional[str] = None,
    ) -> QueueEntry:
        if position is None:
            position = await self.next_position(session, queue_id=queue_id)
        row = QueueEntry(
            queue_id=queue_id,
            player_id=player_id,
            ign=ign,
            kit_id=kit_id,
            position=position,
            status=QUEUE_ENTRY_WAITING,
            joined_at=joined_at or datetime.now(timezone.utc),
            username=username,
        )
        session.add(row)
        await session.flush()
        return row

    async def next_position(self, session: AsyncSession, *, queue_id: int) -> int:
        result = await session.execute(
            select(func.max(QueueEntry.position)).where(QueueEntry.queue_id == queue_id)
        )
        return (result.scalar_one() or 0) + 1

    async def waiting_count(self, session: AsyncSession, *, queue_id: int) -> int:
        result = await session.execute(
            select(func.count(QueueEntry.id)).where(
                QueueEntry.queue_id == queue_id,
                QueueEntry.status == QUEUE_ENTRY_WAITING,
            )
        )
        return int(result.scalar_one() or 0)

    async def get_waiting(
        self, session: AsyncSession, *, queue_id: int, player_id: int
    ) -> Optional[QueueEntry]:
        result = await session.execute(
            select(QueueEntry).where(
                QueueEntry.queue_id == queue_id,
                QueueEntry.player_id == player_id,
                QueueEntry.status == QUEUE_ENTRY_WAITING,
            )
        )
        return result.scalar_one_or_none()

    async def list_waiting(self, session: AsyncSession, *, queue_id: int) -> list[QueueEntry]:
        result = await session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.queue_id == queue_id,
                QueueEntry.status == QUEUE_ENTRY_WAITING,
            )
            .order_by(QueueEntry.position)
        )
        return list(result.scalars())

    async def list_waiting_for_player(
        self, session: AsyncSession, *, player_id: int
    ) -> list[QueueEntry]:
        """Player waiting in any active queue (JSON queue.json is global)."""
        result = await session.execute(
            select(QueueEntry)
            .where(
                QueueEntry.player_id == player_id,
                QueueEntry.status == QUEUE_ENTRY_WAITING,
            )
            .order_by(QueueEntry.position)
        )
        return list(result.scalars())

    async def list_by_status(
        self, session: AsyncSession, *, player_id: int, status: str
    ) -> list[QueueEntry]:
        """Player entries by status, newest first (pulled/left/tested history)."""
        result = await session.execute(
            select(QueueEntry)
            .where(QueueEntry.player_id == player_id, QueueEntry.status == status)
            .order_by(
                func.coalesce(QueueEntry.pulled_at, QueueEntry.joined_at).desc()
            )
        )
        return list(result.scalars())

    async def claim_next_waiting(
        self, session: AsyncSession, *, queue_id: int, pulled_at: Optional[datetime] = None
    ) -> Optional[QueueEntry]:
        """Atomicky vyber a vytažne PRVNÍHO čekajícího hráče fronty.

        Concurrency-safe by construction, unlike "SELECT the first waiting
        row, then UPDATE it":

        * ``FOR UPDATE SKIP LOCKED`` means two concurrent pullers get two
          *different* players instead of both reading the same row and racing
          to update it (which let the same player be pulled twice — the loser
          simply overwrote the winner's ``status``/``pulled_at``);
        * ``ORDER BY position, id`` is the queue order, so the pull is FIFO
          and not "whatever the planner happened to return";
        * a row that is no longer ``waiting`` can never be claimed, so a
          player who left in the meantime is never dragged out of a queue they
          are not in.

        Returns ``None`` when the queue has nobody waiting (or every candidate
        was momentarily locked by a concurrent puller).
        """
        candidate = (
            select(QueueEntry.id)
            .where(
                QueueEntry.queue_id == queue_id,
                QueueEntry.status == QUEUE_ENTRY_WAITING,
            )
            .order_by(QueueEntry.position, QueueEntry.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        stmt = (
            update(QueueEntry)
            .where(QueueEntry.id == candidate.scalar_subquery())
            .values(
                status=QUEUE_ENTRY_PULLED,
                pulled_at=pulled_at or datetime.now(timezone.utc),
            )
            .returning(QueueEntry)
        )
        result = await session.execute(stmt)
        await session.flush()
        return result.scalar_one_or_none()

    async def transition(
        self,
        session: AsyncSession,
        *,
        entry_id: int,
        status: str,
        pulled_at: Optional[datetime] = None,
        room_channel_id: Optional[int] = None,
        removed_at: Optional[datetime] = None,
        removed_reason: Optional[str] = None,
    ) -> Optional[QueueEntry]:
        row = await session.get(QueueEntry, entry_id)
        if row is None:
            return None
        row.status = status
        if pulled_at is not None:
            row.pulled_at = pulled_at
        if room_channel_id is not None:
            row.room_channel_id = room_channel_id
        if removed_at is not None:
            row.removed_at = removed_at
        if removed_reason is not None:
            row.removed_reason = removed_reason
        await session.flush()
        return row

class QueueTesterRepository:
    """Queue-scoped testers (opener + /queue joinasqueue members).

    The opener is the oldest tester in the queue; on leave the role passes
    to the first remaining tester (matches JSON ``active_queues`` takeover).
    """

    async def add(
        self, session: AsyncSession, *, queue_id: int, player_id: int
    ) -> QueueTester:
        row = QueueTester(queue_id=queue_id, player_id=player_id)
        session.add(row)
        await session.flush()
        return row

    async def find(
        self, session: AsyncSession, *, queue_id: int, player_id: int
    ) -> Optional[QueueTester]:
        result = await session.execute(
            select(QueueTester).where(
                QueueTester.queue_id == queue_id,
                QueueTester.player_id == player_id,
            )
        )
        return result.scalar_one_or_none()

    async def list(self, session: AsyncSession, *, queue_id: int) -> list[QueueTester]:
        result = await session.execute(
            select(QueueTester)
            .where(QueueTester.queue_id == queue_id)
            .order_by(QueueTester.joined_at, QueueTester.id)
        )
        return list(result.scalars())

    async def is_member(
        self, session: AsyncSession, *, queue_id: int, player_id: int
    ) -> bool:
        result = await session.execute(
            select(QueueTester.id)
            .where(
                QueueTester.queue_id == queue_id,
                QueueTester.player_id == player_id,
            )
            .limit(1)
        )
        return result.first() is not None

    async def remove(
        self, session: AsyncSession, *, queue_id: int, player_id: int
    ) -> bool:
        row = await self.find(session, queue_id=queue_id, player_id=player_id)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True

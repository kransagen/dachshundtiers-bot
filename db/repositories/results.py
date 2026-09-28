"""Result records (ticket / queue / HT-fight) with promotion state machine."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Result

PROMOTION_DISCORD_PENDING = "discord_pending"
PROMOTION_DISCORD_FAILED = "discord_failed"
PROMOTION_COMMITTED = "committed"
PROMOTION_DB_FAILED_OUTBOXED = "db_failed_outboxed"

ANNOUNCEMENT_PENDING = "pending"
ANNOUNCEMENT_SENT = "sent"
ANNOUNCEMENT_FAILED = "failed"
ANNOUNCEMENT_STATUSES = frozenset(
    {ANNOUNCEMENT_PENDING, ANNOUNCEMENT_SENT, ANNOUNCEMENT_FAILED}
)


class ResultRepository:
    """Results with the Discord-confirmed promotion lifecycle."""

    async def insert(
        self,
        session: AsyncSession,
        *,
        result_key: str,
        kind: str,
        player_id: int,
        kit_id: int,
        subtype: Optional[str] = None,
        evaluator_id: Optional[int] = None,
        ticket_channel_id: Optional[int] = None,
        previous_tier_id: Optional[int] = None,
        new_tier_id: Optional[int] = None,
        bridge_tier_id: Optional[int] = None,
        tier_status: Optional[str] = None,
        score: Optional[str] = None,
        outcome: Optional[str] = None,
        opponent_id: Optional[int] = None,
        opponent_name: Optional[str] = None,
        notes: Optional[str] = None,
        eval_flag: bool = False,
        date: Optional[str] = None,
        recorded_at: Optional[datetime] = None,
        promotion_status: Optional[str] = None,
        announcement_status: Optional[str] = None,
    ) -> Result:
        row = Result(
            result_key=result_key,
            kind=kind,
            subtype=subtype,
            player_id=player_id,
            evaluator_id=evaluator_id,
            kit_id=kit_id,
            ticket_channel_id=ticket_channel_id,
            previous_tier_id=previous_tier_id,
            new_tier_id=new_tier_id,
            bridge_tier_id=bridge_tier_id,
            tier_status=tier_status,
            score=score,
            outcome=outcome,
            opponent_id=opponent_id,
            opponent_name=opponent_name,
            notes=notes,
            eval_flag=eval_flag,
            date=date,
            recorded_at=recorded_at or datetime.now(timezone.utc),
            promotion_status=promotion_status,
            announcement_status=announcement_status,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_by_key(
        self, session: AsyncSession, result_key: str
    ) -> Optional[Result]:
        result = await session.execute(
            select(Result).where(Result.result_key == result_key)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, result_id: int) -> Optional[Result]:
        return await session.get(Result, result_id)

    async def set_promotion_status(
        self, session: AsyncSession, *, result_key: str, promotion_status: str
    ) -> Optional[Result]:
        row = await self.get_by_key(session, result_key)
        if row is None:
            return None
        row.promotion_status = promotion_status
        await session.flush()
        return row

    async def mark_outboxed(
        self, session: AsyncSession, *, result_key: str
    ) -> Optional[Result]:
        return await self.set_promotion_status(
            session, result_key=result_key, promotion_status=PROMOTION_DB_FAILED_OUTBOXED
        )

    async def set_announcement(
        self,
        session: AsyncSession,
        *,
        result_key: str,
        announcement_status: str,
        announcement_message_id: Optional[int] = None,
    ) -> Optional[Result]:
        row = await self.get_by_key(session, result_key)
        if row is None:
            return None
        row.announcement_status = announcement_status
        if announcement_message_id is not None:
            row.announcement_message_id = announcement_message_id
        await session.flush()
        return row

    async def list_for_player(
        self, session: AsyncSession, *, player_id: int, limit: int = 100
    ) -> list[Result]:
        result = await session.execute(
            select(Result)
            .where(Result.player_id == player_id)
            .order_by(Result.recorded_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_all(
        self, session: AsyncSession, *, limit: int = 1000
    ) -> list[Result]:
        result = await session.execute(
            select(Result)
            .order_by(Result.recorded_at.asc(), Result.id.asc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_free_ht_fights(
        self, session: AsyncSession, *, player_id: int, since: datetime
    ) -> list[Result]:
        result = await session.execute(
            select(Result)
            .where(
                Result.kind == "ht_fight",
                Result.player_id == player_id,
                Result.ticket_channel_id.is_(None),
                Result.recorded_at >= since,
            )
            .order_by(Result.recorded_at.desc())
        )
        return list(result.scalars())

    async def list_pending_commits(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[Result]:
        result = await session.execute(
            select(Result)
            .where(Result.promotion_status == PROMOTION_DISCORD_PENDING)
            .order_by(Result.recorded_at.asc())
            .limit(limit)
        )
        return list(result.scalars())
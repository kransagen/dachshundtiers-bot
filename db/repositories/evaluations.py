"""Evaluation + tester repositories (grant/revoke lifecycle)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Evaluation, Tester


class EvaluationRepository:
    """Active eval marks: one per (player, kit) while not revoked."""

    async def grant(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        kit_id: int,
        granted_by: Optional[int] = None,
        granted_at: Optional[datetime] = None,
    ) -> Evaluation:
        row = Evaluation(
            player_id=player_id,
            kit_id=kit_id,
            granted_by=granted_by,
            granted_at=granted_at or datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row

    async def revoke(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        kit_id: int,
        revoked_at: Optional[datetime] = None,
    ) -> bool:
        active = await self.get_active(session, player_id=player_id, kit_id=kit_id)
        if active is None:
            return False
        active.revoked_at = revoked_at or datetime.now(timezone.utc)
        await session.flush()
        return True

    async def get_active(
        self, session: AsyncSession, *, player_id: int, kit_id: int
    ) -> Optional[Evaluation]:
        result = await session.execute(
            select(Evaluation).where(
                Evaluation.player_id == player_id,
                Evaluation.kit_id == kit_id,
                Evaluation.revoked_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def has_active(
        self, session: AsyncSession, *, player_id: int, kit_id: int
    ) -> bool:
        result = await session.execute(
            select(Evaluation.id)
            .where(
                Evaluation.player_id == player_id,
                Evaluation.kit_id == kit_id,
                Evaluation.revoked_at.is_(None),
            )
            .limit(1)
        )
        return result.first() is not None

    async def list_active(
        self, session: AsyncSession, *, player_id: Optional[int] = None
    ) -> list[Evaluation]:
        stmt = select(Evaluation).where(Evaluation.revoked_at.is_(None))
        if player_id is not None:
            stmt = stmt.where(Evaluation.player_id == player_id)
        result = await session.execute(
            stmt.order_by(Evaluation.granted_at.desc())
        )
        return list(result.scalars())


class TesterRepository:
    """Global tester flag (player row is the PK)."""

    async def grant(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        granted_by: Optional[int] = None,
        granted_at: Optional[datetime] = None,
    ) -> Tester:
        row = Tester(
            player_id=player_id,
            granted_by=granted_by,
            granted_at=granted_at or datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row

    async def revoke(self, session: AsyncSession, *, player_id: int) -> bool:
        row = await session.get(Tester, player_id)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True

    async def is_tester(self, session: AsyncSession, *, player_id: int) -> bool:
        return await session.get(Tester, player_id) is not None

    async def list(self, session: AsyncSession) -> list[Tester]:
        result = await session.execute(select(Tester).order_by(Tester.granted_at.desc()))
        return list(result.scalars())
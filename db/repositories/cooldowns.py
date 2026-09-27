"""Cooldown repository (waitlist / ht3) with PostgreSQL upserts."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import text

from db.models import Cooldown

COOLDOWN_WAITLIST = "waitlist"
COOLDOWN_HT3 = "ht3"


class CooldownRepository:
    """Per-player cooldowns; partial unique indexes make waitlist (kit NULL)
    and kit-bound variants mutually exclusive upsert targets."""

    async def upsert(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        cooldown_type: str,
        expires_at: datetime,
        kit_id: Optional[int] = None,
        source: str = "auto",
    ) -> Cooldown:
        stmt = pg_insert(Cooldown).values(
            player_id=player_id,
            cooldown_type=cooldown_type,
            kit_id=kit_id,
            expires_at=expires_at,
            source=source,
        )
        if cooldown_type == COOLDOWN_WAITLIST and kit_id is None:
            stmt = stmt.on_conflict_do_update(
                index_elements=["player_id"],
                index_where=text(
                    "cooldown_type = 'waitlist' AND kit_id IS NULL"
                ),
                set_={"expires_at": expires_at, "source": source},
            )
        else:
            stmt = stmt.on_conflict_do_update(
                index_elements=["player_id", "kit_id", "cooldown_type"],
                index_where=text("kit_id IS NOT NULL"),
                set_={"expires_at": expires_at, "source": source},
            )
        stmt = stmt.returning(Cooldown).execution_options(populate_existing=True)
        row = (await session.execute(stmt)).scalar_one()
        await session.flush()
        return row

    async def get_active(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        cooldown_type: Optional[str] = None,
        kit_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> list[Cooldown]:
        now = now if now is not None else func.now()
        stmt = select(Cooldown).where(
            Cooldown.player_id == player_id,
            Cooldown.expires_at > now,
        )
        if cooldown_type is not None:
            stmt = stmt.where(Cooldown.cooldown_type == cooldown_type)
        if kit_id is not None:
            stmt = stmt.where(Cooldown.kit_id == kit_id)
        result = await session.execute(stmt.order_by(Cooldown.expires_at.desc()))
        return list(result.scalars())

    async def is_active(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        cooldown_type: str,
        kit_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        now = now if now is not None else func.now()
        stmt = select(Cooldown.id).where(
            Cooldown.player_id == player_id,
            Cooldown.cooldown_type == cooldown_type,
            Cooldown.expires_at > now,
        )
        if kit_id is not None:
            stmt = stmt.where(Cooldown.kit_id == kit_id)
        result = await session.execute(stmt.limit(1))
        return result.first() is not None

    async def delete(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        cooldown_type: Optional[str] = None,
        kit_id: Optional[int] = None,
    ) -> int:
        stmt = delete(Cooldown).where(Cooldown.player_id == player_id)
        if cooldown_type is not None:
            stmt = stmt.where(Cooldown.cooldown_type == cooldown_type)
        if kit_id is not None:
            stmt = stmt.where(Cooldown.kit_id == kit_id)
        result = await session.execute(stmt)
        return result.rowcount or 0
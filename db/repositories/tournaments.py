"""Tournament repository (create / signup / end / delete lifecycle).

Phase F (F2/F3): relational replacement for ``tournaments.json``. Semantics
mirror the legacy file exactly: a tournament row lives until it is deleted
(/deleteturnaj), and while any row exists for a kit, creating a new
tournament for that kit is blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Player, Tournament, TournamentEntry


class TournamentRepository:
    async def get(self, session: AsyncSession, tournament_id: int) -> Optional[Tournament]:
        return await session.get(Tournament, tournament_id)

    async def get_by_kit(
        self, session: AsyncSession, *, kit_id: int
    ) -> Optional[Tournament]:
        result = await session.execute(
            select(Tournament)
            .where(Tournament.kit_id == kit_id)
            .order_by(Tournament.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_all(self, session: AsyncSession) -> list[Tournament]:
        """All rows (active + ended) — end-of-life is /deleteturnaj."""
        result = await session.execute(select(Tournament).order_by(Tournament.created_at))
        return list(result.scalars())

    async def create(
        self,
        session: AsyncSession,
        *,
        kit_id: int,
        name: str,
        tier: str,
        groups_count: int,
        category_id: int,
        signup_channel_id: int,
        signup_message_id: int,
        role_id: int,
        guild_id: int,
        deadline: datetime,
    ) -> Tournament:
        row = Tournament(
            kit_id=kit_id,
            name=name,
            tier=tier,
            groups_count=groups_count,
            category_id=category_id,
            signup_channel_id=signup_channel_id,
            signup_message_id=signup_message_id,
            role_id=role_id,
            guild_id=guild_id,
            deadline=deadline,
        )
        session.add(row)
        await session.flush()
        return row

    async def mark_ended(
        self, session: AsyncSession, tournament_id: int, *, ended: bool = True
    ) -> None:
        row = await self.get(session, tournament_id)
        if row is None:
            return
        row.ended = ended
        await session.flush()

    async def is_participant(
        self, session: AsyncSession, *, tournament_id: int, player_id: int
    ) -> bool:
        result = await session.execute(
            select(TournamentEntry.id)
            .where(
                TournamentEntry.tournament_id == tournament_id,
                TournamentEntry.player_id == player_id,
            )
            .limit(1)
        )
        return result.first() is not None

    async def add_participant(
        self,
        session: AsyncSession,
        *,
        tournament_id: int,
        player_id: int,
        joined_at: Optional[datetime] = None,
    ) -> TournamentEntry:
        row = TournamentEntry(
            tournament_id=tournament_id,
            player_id=player_id,
            joined_at=joined_at or datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row

    async def list_participant_ids(
        self, session: AsyncSession, tournament_id: int
    ) -> list[int]:
        result = await session.execute(
            select(TournamentEntry.player_id)
            .where(TournamentEntry.tournament_id == tournament_id)
            .order_by(TournamentEntry.joined_at)
        )
        return list(result.scalars())

    async def list_participant_discord_ids(
        self, session: AsyncSession, tournament_id: int
    ) -> list[int]:
        result = await session.execute(
            select(Player.discord_id)
            .join(TournamentEntry, TournamentEntry.player_id == Player.id)
            .where(
                TournamentEntry.tournament_id == tournament_id,
                Player.discord_id.is_not(None),
            )
            .order_by(TournamentEntry.joined_at, TournamentEntry.id)
        )
        return list(result.scalars())

    async def count_participants(self, session: AsyncSession, tournament_id: int) -> int:
        result = await session.execute(
            select(func.count())
            .select_from(TournamentEntry)
            .where(TournamentEntry.tournament_id == tournament_id)
        )
        return result.scalar_one()

    async def delete(self, session: AsyncSession, tournament_id: int) -> bool:
        await session.execute(
            delete(TournamentEntry).where(TournamentEntry.tournament_id == tournament_id)
        )
        row = await self.get(session, tournament_id)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True
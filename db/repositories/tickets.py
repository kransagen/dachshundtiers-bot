"""Ticket + ticket-member repositories (channel-backed eval/fight tickets)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Ticket, TicketMember

TICKET_OPEN = "open"
TICKET_CLOSED = "closed"

TICKET_TYPE_EVAL = "eval"
TICKET_TYPE_FIGHT = "fight"


class TicketRepository:
    """Ticket lifecycle: open (unique per open player+kit), close, claim."""

    async def open(
        self,
        session: AsyncSession,
        *,
        channel_id: int,
        player_id: int,
        ign: str,
        kit_id: int,
        target_tier_id: Optional[int] = None,
        current_tier_id: Optional[int] = None,
        eval: bool = False,
        ticket_type: str = TICKET_TYPE_EVAL,
        created_at: Optional[datetime] = None,
        owner_name: Optional[str] = None,
        category_id: Optional[int] = None,
        panel_message_id: Optional[int] = None,
    ) -> Ticket:
        row = Ticket(
            channel_id=channel_id,
            player_id=player_id,
            ign=ign,
            kit_id=kit_id,
            target_tier_id=target_tier_id,
            current_tier_id=current_tier_id,
            eval=eval,
            ticket_type=ticket_type,
            status=TICKET_OPEN,
            created_at=created_at or datetime.now(timezone.utc),
            owner_name=owner_name,
            category_id=category_id,
            panel_message_id=panel_message_id,
        )
        session.add(row)
        await session.flush()
        return row

    async def get_by_channel(
        self, session: AsyncSession, channel_id: int
    ) -> Optional[Ticket]:
        result = await session.execute(
            select(Ticket).where(Ticket.channel_id == channel_id)
        )
        return result.scalar_one_or_none()

    async def get_by_channel_for_update(
        self, session: AsyncSession, channel_id: int
    ) -> Optional[Ticket]:
        """Načte ticket s řádkovým zámkem (``SELECT ... FOR UPDATE``).

        H9 audit fix: souběžné claimy dvou testerů se serializují tady — druhý
        ``SELECT ... FOR UPDATE`` čeká na commit/rollback prvního a v READ
        COMMITTED pak přečte nejnovější verzi řádku (včetně cizího claimera),
        takže už nikdy neprojde přes kontrolu ``claimer_id IS NULL``.
        """
        result = await session.execute(
            select(Ticket)
            .where(Ticket.channel_id == channel_id)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, ticket_id: int) -> Optional[Ticket]:
        return await session.get(Ticket, ticket_id)

    async def close(
        self, session: AsyncSession, *, ticket_id: int, closed_at: Optional[datetime] = None
    ) -> Optional[Ticket]:
        row = await self.get_by_id(session, ticket_id)
        if row is None:
            return None
        row.status = TICKET_CLOSED
        row.closed_at = closed_at or datetime.now(timezone.utc)
        await session.flush()
        return row

    async def close_by_channel(
        self, session: AsyncSession, *, channel_id: int, closed_at: Optional[datetime] = None
    ) -> Optional[Ticket]:
        row = await self.get_by_channel(session, channel_id)
        if row is None:
            return None
        row.status = TICKET_CLOSED
        row.closed_at = closed_at or datetime.now(timezone.utc)
        await session.flush()
        return row

    async def claim(
        self,
        session: AsyncSession,
        *,
        ticket_id: int,
        claimer_id: Optional[int],
        claimer_name: Optional[str] = None,
    ) -> Optional[Ticket]:
        row = await self.get_by_id(session, ticket_id)
        if row is None:
            return None
        row.claimer_id = claimer_id
        row.claimer_name = claimer_name
        await session.flush()
        return row

    async def list_open(
        self,
        session: AsyncSession,
        *,
        player_id: Optional[int] = None,
        kit_id: Optional[int] = None,
    ) -> list[Ticket]:
        stmt = select(Ticket).where(Ticket.status == TICKET_OPEN)
        if player_id is not None:
            stmt = stmt.where(Ticket.player_id == player_id)
        if kit_id is not None:
            stmt = stmt.where(Ticket.kit_id == kit_id)
        result = await session.execute(stmt.order_by(Ticket.created_at))
        return list(result.scalars())

    async def list_all(self, session: AsyncSession) -> list[Ticket]:
        result = await session.execute(
            select(Ticket).order_by(Ticket.created_at, Ticket.id)
        )
        return list(result.scalars())

    async def set_panel_message(
        self, session: AsyncSession, *, channel_id: int, message_id: int
    ) -> Optional[Ticket]:
        row = await self.get_by_channel(session, channel_id)
        if row is None:
            return None
        row.panel_message_id = int(message_id)
        await session.flush()
        return row

    async def reopen_by_channel(
        self, session: AsyncSession, *, channel_id: int
    ) -> Optional[Ticket]:
        row = await self.get_by_channel(session, channel_id)
        if row is None or row.status != TICKET_CLOSED:
            return None
        row.status = TICKET_OPEN
        row.closed_at = None
        await session.flush()
        return row


class TicketMemberRepository:
    """Ticket participant list (hard-stuck / witnesses)."""

    async def add(
        self,
        session: AsyncSession,
        *,
        ticket_id: int,
        player_id: int,
        added_at: Optional[datetime] = None,
    ) -> TicketMember:
        row = TicketMember(
            ticket_id=ticket_id,
            player_id=player_id,
            added_at=added_at or datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
        return row

    async def remove(
        self,
        session: AsyncSession,
        *,
        ticket_id: int,
        player_id: int,
        removed_at: Optional[datetime] = None,
    ) -> bool:
        row = await self.get(session, ticket_id=ticket_id, player_id=player_id)
        if row is None or row.removed_at is not None:
            return False
        row.removed_at = removed_at or datetime.now(timezone.utc)
        await session.flush()
        return True

    async def get(
        self, session: AsyncSession, *, ticket_id: int, player_id: int
    ) -> Optional[TicketMember]:
        result = await session.execute(
            select(TicketMember).where(
                TicketMember.ticket_id == ticket_id,
                TicketMember.player_id == player_id,
                TicketMember.removed_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_ticket(
        self, session: AsyncSession, *, ticket_id: int
    ) -> list[TicketMember]:
        result = await session.execute(
            select(TicketMember)
            .where(TicketMember.ticket_id == ticket_id)
            .order_by(TicketMember.added_at)
        )
        return list(result.scalars())

    async def has_member(
        self, session: AsyncSession, *, ticket_id: int, player_id: int
    ) -> bool:
        return await self.get(session, ticket_id=ticket_id, player_id=player_id) is not None
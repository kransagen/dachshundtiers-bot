"""Ticket models (channel-backed eval/fight tickets + member lists)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    owner_name: Mapped[Optional[str]] = mapped_column(Text)
    ign: Mapped[str] = mapped_column(Text, nullable=False)
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    target_tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    current_tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    eval: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    claimer_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("players.id")
    )
    claimer_name: Mapped[Optional[str]] = mapped_column(Text)
    category_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    panel_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    ticket_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'eval'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_tickets_open_player_kit",
            "player_id",
            "kit_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_tickets_claimer", "claimer_id"),
        CheckConstraint("status IN ('open', 'closed')", name="status"),
        CheckConstraint(
            "ticket_type IN ('eval', 'fight')", name="ticket_type"
        ),
    )


class TicketMember(Base):
    __tablename__ = "ticket_members"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("ticket_id", "player_id", "added_at", name="uq_ticket_member"),
        # H10 audit fix: at most ONE active membership per (ticket, player).
        # ``added_at`` differs between re-adds, so the time-based unique above
        # cannot stop two concurrent /add operations from inserting two ACTIVE
        # rows — this partial unique index rejects the loser at the DB level,
        # while historical re-adds after removal (removed_at IS NOT NULL) stay
        # untouched.
        Index(
            "uq_ticket_members_active",
            "ticket_id",
            "player_id",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
    )
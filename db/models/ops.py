"""Operational models: cooldowns, queues, queue entries, evals, testers."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Cooldown(Base):
    __tablename__ = "cooldowns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    cooldown_type: Mapped[str] = mapped_column(Text, nullable=False)
    kit_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("kits.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'auto'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "uq_cooldowns_waitlist",
            "player_id",
            unique=True,
            postgresql_where=text(
                "cooldown_type = 'waitlist' AND kit_id IS NULL"
            ),
        ),
        Index(
            "uq_cooldowns_kit",
            "player_id",
            "kit_id",
            "cooldown_type",
            unique=True,
            postgresql_where=text("kit_id IS NOT NULL"),
        ),
        Index("ix_cooldowns_expiry", "expires_at"),
        CheckConstraint(
            "cooldown_type IN ('waitlist', 'ht3')", name="type"
        ),
    )


class Queue(Base):
    __tablename__ = "queues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    panel_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    panel_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    __table_args__ = (
        Index(
            "uq_queue_active_kit",
            "kit_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )


class QueueEntry(Base):
    __tablename__ = "queue_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queues.id"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    ign: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(Text)
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'waiting'")
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    pulled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    room_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    removed_reason: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index(
            "uq_queue_waiting_player",
            "queue_id",
            "player_id",
            unique=True,
            postgresql_where=text("status = 'waiting'"),
        ),
        Index("ix_queue_position", "queue_id", "position"),
        CheckConstraint(
            "status IN ('waiting', 'pulled', 'left', 'tested')",
            name="status",
        ),
    )


class Evaluation(Base):
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    granted_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_eval_active",
            "player_id",
            "kit_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class Tester(Base):
    __tablename__ = "testers"

    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), primary_key=True
    )
    granted_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class QueueTester(Base):
    __tablename__ = "queue_testers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queues.id"), nullable=False
    )
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("uq_queue_tester_player", "queue_id", "player_id", unique=True),
    )
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

from db.base import Base, utcnow


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
        # The naming convention (`db/base.py`: "ck": "ck_%(table_name)s_%(constraint_name)s")
        # expands this to `ck_cooldowns_type`, which is exactly what migration
        # 63bdbcfda74a creates. The bare suffix is the correct declaration —
        # spelling the full name here would yield `ck_cooldowns_ck_cooldowns_type`.
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
        Index("ix_queue_entries_player", "player_id"),
        CheckConstraint(
            "status IN ('waiting', 'pulled', 'left', 'tested')",
            name="status",
        ),
        CheckConstraint("position >= 0", name="position"),
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


class TesterRoom(Base):
    """The one Discord channel that is a given tester's room.

    ``/mktesterroom`` writes this row; ``/queue pull <kit>`` and the panel pull
    button read it to find where to grant the pulled player access. The tester
    (Discord user id) is the primary key because the business rule is exactly
    one tester room per tester, reused for every kit that tester pulls from.
    ``channel_id`` is UNIQUE because a room belongs to at most one tester.

    The channel id is a *projection* target, not the source of truth: the row
    here is authoritative, the remote channel merely has to exist when the
    pull runs.
    """

    __tablename__ = "tester_rooms"

    tester_discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

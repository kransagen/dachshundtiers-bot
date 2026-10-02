"""Result records (ticket / queue / HT-fight) with promotion state machine."""

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
    desc,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Result(Base):
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    result_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subtype: Mapped[Optional[str]] = mapped_column(Text)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    evaluator_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("players.id")
    )
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    ticket_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    previous_tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    new_tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    bridge_tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    tier_status: Mapped[Optional[str]] = mapped_column(Text)
    score: Mapped[Optional[str]] = mapped_column(Text)
    outcome: Mapped[Optional[str]] = mapped_column(Text)
    opponent_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    opponent_name: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    eval_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    date: Mapped[Optional[str]] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    promotion_status: Mapped[Optional[str]] = mapped_column(Text)
    announcement_status: Mapped[Optional[str]] = mapped_column(Text)
    announcement_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_results_player_kit_time",
            "player_id",
            "kit_id",
            desc("recorded_at"),
        ),
        Index("ix_results_ticket", "ticket_channel_id"),
        Index("ix_results_evaluator", "evaluator_id"),
        Index(
            "ix_results_promo",
            "promotion_status",
            postgresql_where=text("promotion_status IS NOT NULL"),
        ),
        CheckConstraint(
            "kind IN ('ticket', 'queue', 'ht_fight')",
            name="kind",
        ),
        CheckConstraint(
            "promotion_status IS NULL OR promotion_status IN "
            "('discord_pending', 'discord_failed', 'committed', 'db_failed_outboxed')",
            name="promotion_status",
        ),
        CheckConstraint(
            "announcement_status IS NULL OR announcement_status IN ('pending', 'sent', 'failed')",
            name="announcement_status",
        ),
    )
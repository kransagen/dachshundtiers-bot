"""Tester stat credits (admin manual backfill, /addtest).

Phase F (#4): ``testers_stats.json`` is derived at runtime from ``results``
(per-tester month tallies). ``/addtest`` credits historical tests that are
NOT backed by real result rows, so they cannot be derived — this ledger
preserves that functionality. Statistics merge: Result aggregation for
kits/tiers/lastTested/hourly and Result counts + credits for total/monthly.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TesterCredit(Base):
    __tablename__ = "tester_credits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tester_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    month: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tester_id", "month", name="uq_tester_credit_month"
        ),
    )
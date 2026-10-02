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
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class TesterCredit(Base):
    __tablename__ = "tester_credits"

    # Název třídy začíná na ``Test``, takže by ho pytestbral jako testovací
    # třídu (PytestCollectionWarning) a nešlo by ho vůbec spustit.
    __test__ = False

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
        # Declared as a unique *Index*, not a UniqueConstraint, because that is
        # what migration c4d5e6f7a8b9 actually created. Declaring a
        # UniqueConstraint here made every Alembic autogenerate run emit a
        # phantom `remove_index` + `add_constraint` pair for a table that had
        # not changed. The database is the correct side here: a unique index
        # enforces the same thing, and the deployed schema already has it, so
        # the model is what needed fixing (mirroring the same reasoning as
        # `db/models/ops.py:Cooldown`).
        Index("uq_tester_credit_month", "tester_id", "month", unique=True),
    )
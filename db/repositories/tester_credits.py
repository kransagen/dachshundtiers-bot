"""TesterCredit persistence (admin manual backfill ledger)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TesterCredit


class TesterCreditRepository:
    async def credit(
        self, session: AsyncSession, *, tester_id: int, month: str, amount: int
    ) -> TesterCredit:
        """Accumulate ``amount`` for (tester, month) — one row per month."""
        result = await session.execute(
            select(TesterCredit).where(
                TesterCredit.tester_id == tester_id,
                TesterCredit.month == month,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.amount += amount
            return row
        row = TesterCredit(tester_id=tester_id, month=month, amount=amount)
        session.add(row)
        return row

    async def totals(self, session: AsyncSession, tester_id: int) -> dict[str, int]:
        """Total credits and per-month credits for a tester."""
        result = await session.execute(
            select(TesterCredit).where(TesterCredit.tester_id == tester_id)
        )
        total = 0
        monthly: dict[str, int] = {}
        for row in result.scalars():
            total += row.amount
            monthly[row.month] = monthly.get(row.month, 0) + row.amount
        return {"total": total, "monthly": monthly}

    async def sum_for_month(
        self, session: AsyncSession, month: str
    ) -> dict[int, int]:
        """Credited amounts per tester for one month (leaderboard merge)."""
        result = await session.execute(
            select(TesterCredit).where(TesterCredit.month == month)
        )
        out: dict[int, int] = {}
        for row in result.scalars():
            out[row.tester_id] = out.get(row.tester_id, 0) + row.amount
        return out
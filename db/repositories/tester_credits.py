"""TesterCredit persistence (admin manual backfill ledger)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import TesterCredit


class TesterCreditRepository:
    async def credit(
        self, session: AsyncSession, *, tester_id: int, month: str, amount: int
    ) -> TesterCredit:
        """Accumulate ``amount`` for (tester, month) — one row per month.

        A single ``INSERT … ON CONFLICT DO UPDATE`` so two concurrent credits
        for the same month add up instead of one overwriting the other.
        """
        stmt = (
            pg_insert(TesterCredit)
            .values(tester_id=tester_id, month=month, amount=amount)
            .on_conflict_do_update(
                index_elements=[TesterCredit.tester_id, TesterCredit.month],
                set_={"amount": TesterCredit.amount + amount},
            )
            .returning(TesterCredit.id)
        )
        row_id = (await session.execute(stmt)).scalar_one()
        row = await session.get(TesterCredit, row_id, populate_existing=True)
        return row

    async def lock_month(
        self, session: AsyncSession, *, tester_id: int, month: str
    ) -> TesterCredit:
        """Ensure the (tester, month) row exists and lock it for update."""
        await self.credit(session, tester_id=tester_id, month=month, amount=0)
        result = await session.execute(
            select(TesterCredit)
            .where(TesterCredit.tester_id == tester_id, TesterCredit.month == month)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one()

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
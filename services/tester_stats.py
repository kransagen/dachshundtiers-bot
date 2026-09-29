"""Tester statistics, derived from PostgreSQL.

Phase F (#4): statistiky se počítají za běhu z ``results`` (per-tester totals
pro kity/tiery/měsíce/hodiny). ``/addtest`` ruční kredity jdou do ledgeru
``tester_credits`` a slučují se do téhož projekce, takže admin backfill funguje
bez JSON zdroje.

DRUHÝ REŽIM TU UŽ NENÍ. ``testers_stats.json`` se už nečte ani nezapisuje a
každá funkce vyžaduje ``session_factory``. Bez DB statistiky nejsou – nejde
vrátit „nějaký odhad ze souboru", protože by to byl druhý zdroj pravdy, který
by se tiš rozcházel s výsledky. Klíče v návratových dictech (``total``,
``lastTested``, ``kits``, ``tiers``, ``monthly``, ``hourlyLogs``) zůstávají,
protože je čtou cogy.

``kind`` se počítá jen ticket/queue – ``ht_fight`` se nikdy nepočítal ani v
JSONu, a to je záměrné, ne opomenutí.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Kit, Player, Result, TesterCredit, TierDefinition
from db.repositories.players import PlayerRepository
from db.repositories.tester_credits import TesterCreditRepository
from db.services.session import transaction as db_transaction

PRAGUE = ZoneInfo("Europe/Prague")
RESULT_STAT_KINDS = ("ticket", "queue")


def _tier_display(tier_name: Optional[str], eval_flag: bool) -> Optional[str]:
    if tier_name is None:
        return None
    return f"{tier_name} + eval" if eval_flag else tier_name


async def tester_stats(
    player_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[dict]:
    """Statistiky jednoho testera, nebo ``None`` když nemá žádné testy."""
    if session_factory is None:
        raise RuntimeError(
            "tester_stats potřebuje PostgreSQL; testers_stats.json se už nepoužívá"
        )
    async with db_transaction(session_factory) as session:
        uid = int(player_id)
        player = await PlayerRepository().get_by_discord_id(session, uid)
        if player is None:
            return None

        rows = await session.execute(
            select(
                Kit.name.label("kit_name"),
                TierDefinition.display_name.label("tier_display"),
                Result.eval_flag,
                Result.recorded_at,
            )
            .join(Kit, Result.kit_id == Kit.id)
            .outerjoin(TierDefinition, Result.new_tier_id == TierDefinition.id)
            .where(
                Result.evaluator_id == player.id,
                Result.kind.in_(RESULT_STAT_KINDS),
            )
        )

        kits: dict[str, int] = {}
        tiers: dict[str, int] = {}
        monthly: dict[str, int] = {}
        hours: list[int] = []
        n_results = 0
        last_ts: Optional[datetime] = None
        for kit_name, tier_display, eval_flag, recorded_at in rows:
            n_results += 1
            kits[kit_name] = kits.get(kit_name, 0) + 1
            display = _tier_display(tier_display, eval_flag)
            if display is not None:
                tiers[display] = tiers.get(display, 0) + 1
            local = recorded_at.astimezone(PRAGUE)
            month = local.strftime("%m.%Y")
            monthly[month] = monthly.get(month, 0) + 1
            hours.append(local.hour)
            if last_ts is None or local > last_ts:
                last_ts = local
        last_tested = last_ts.strftime("%d.%m.%Y") if last_ts is not None else ""

        credits = await TesterCreditRepository().totals(session, player.id)
        if n_results == 0 and credits["total"] == 0:
            return None

        merged_monthly = dict(credits["monthly"])
        for month, count in monthly.items():
            merged_monthly[month] = merged_monthly.get(month, 0) + count

        return {
            "total": n_results + credits["total"],
            "lastTested": last_tested,
            "kits": kits,
            "tiers": tiers,
            "monthly": merged_monthly,
            "hourlyLogs": hours,
        }


async def tester_leaderboard(
    period: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> list:
    """[(tester_key, score)] sestupně, jen score > 0; period all|current."""
    if session_factory is None:
        raise RuntimeError(
            "tester_leaderboard potřebuje PostgreSQL; testers_stats.json se už nepoužívá"
        )
    async with db_transaction(session_factory) as session:
        current_month = datetime.now(PRAGUE).strftime("%m.%Y")
        scores: dict[int, int] = {}

        rows = await session.execute(
            select(Result.evaluator_id, Result.recorded_at, Player.discord_id)
            .join(Player, Player.id == Result.evaluator_id)
            .where(
                Result.kind.in_(RESULT_STAT_KINDS),
                Result.evaluator_id.is_not(None),
            )
        )
        for evaluator_id, recorded_at, discord_id in rows:
            if discord_id is None:
                continue
            if period == "all":
                scores[discord_id] = scores.get(discord_id, 0) + 1
            elif recorded_at.astimezone(PRAGUE).strftime("%m.%Y") == current_month:
                scores[discord_id] = scores.get(discord_id, 0) + 1

        credits = await session.execute(
            select(TesterCredit, Player.discord_id)
            .join(Player, Player.id == TesterCredit.tester_id)
        )
        for row, discord_id in credits:
            if discord_id is None:
                continue
            if period == "all" or row.month == current_month:
                scores[discord_id] = scores.get(discord_id, 0) + row.amount

        entries = [(str(tester_id), score) for tester_id, score in scores.items() if score > 0]
        entries.sort(key=lambda item: item[1], reverse=True)
        return entries


async def credit_tester(
    tester_id: str,
    amount: int,
    month: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ruční připsání historických testů (/addtest) do ledgeru tester_credits."""
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_or_create_shell(
            session, discord_id=int(tester_id)
        )
        await TesterCreditRepository().credit(
            session, tester_id=player.id, month=month, amount=amount
        )


async def remove_tester_credit(
    tester_id: str,
    amount: int,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Odečte ``amount`` z aktuálního měsíce i celkového součtu (/removetest).

    Odečet jde do ledgeru jako záporný kredit aktuálního měsíce, takže sníží
    i počty odvozené z ``results`` – měsíční součet (výsledky + kredity) ale
    nikdy neklesne pod nulu. Řádek měsíce je zamčený (``FOR UPDATE``), takže
    dvě souběžná ``/removetest`` se sečtou místo přepsání.

    Vrací nový celkový počet testů testera (pro hlášku bota).
    """
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(tester_id))
        if player is None:
            return 0
        current = datetime.now(PRAGUE).strftime("%m.%Y")
        repo = TesterCreditRepository()
        row = await repo.lock_month(session, tester_id=player.id, month=current)
        month_results = await _count_stat_results(session, player.id, month=current)
        floor = -month_results
        new_amount = max(floor, row.amount - int(amount))
        await session.execute(
            update(TesterCredit)
            .where(TesterCredit.id == row.id)
            .values(amount=new_amount)
        )
        return await _count_stat_results(session, player.id) + (
            await repo.totals(session, player.id)
        )["total"]


async def _count_stat_results(
    session, player_id: int, *, month: Optional[str] = None
) -> int:
    stmt = select(Result.recorded_at).where(
        Result.evaluator_id == player_id,
        Result.kind.in_(RESULT_STAT_KINDS),
    )
    if month is None:
        result = await session.execute(
            select(func.count()).select_from(stmt.subquery())
        )
        return int(result.scalar_one())
    rows = await session.execute(stmt)
    return sum(
        1 for (recorded_at,) in rows
        if recorded_at.astimezone(PRAGUE).strftime("%m.%Y") == month
    )

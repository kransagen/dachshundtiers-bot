"""Tester statistics — JSON file or derived from PostgreSQL.

Phase F (#4): ``testers_stats.json`` aggregation moves to runtime derivation
from ``results`` (per-tester totals for kits/tiers/months/hours). ``/addtest``
manual credits land in the ``tester_credits`` ledger and merge into the same
projection, so the admin backfill feature survives without a JSON source.

JSON režim: čte/zapisuje ``testers_stats.json`` přesně jako původní cog kód.
DB režim: statistiky se dopočítávají z ``Result`` (jen ``kind`` ticket/queue —
ht_fight se v JSONu nikdy nepočítal) + ``TesterCredit`` pro total/monthly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import IntegrityError

from db.models import Kit, Player, Result, TesterCredit, TierDefinition
from db.repositories.players import PlayerRepository
from db.repositories.tester_credits import TesterCreditRepository
from db.services.session import transaction as db_transaction
from services.store import transaction
from storage import load_data
from utils import month_key

PRAGUE = ZoneInfo("Europe/Prague")
RESULT_STAT_KINDS = ("ticket", "queue")


def _tier_display(tier_name: Optional[str], eval_flag: bool) -> Optional[str]:
    if tier_name is None:
        return None
    return f"{tier_name} + eval" if eval_flag else tier_name


async def tester_stats(
    player_id: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Optional[dict]:
    """Statistiky jednoho testera (JSON-parity klíče) nebo ``None`` bez dat."""
    if session_factory is not None:
        return await _db_tester_stats(session_factory, player_id)
    stats_db = load_data("testers_stats.json", {})
    return stats_db.get(str(player_id))


async def _db_tester_stats(session_factory, player_id: str) -> Optional[dict]:
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
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> list:
    """[(tester_key, score)] sestupně, jen score > 0; period all|current."""
    if session_factory is not None:
        return await _db_tester_leaderboard(session_factory, period)

    stats_db = load_data("testers_stats.json", {})
    current_month = datetime.now().strftime("%m.%Y")
    entries: list = []
    for tester_id, data in stats_db.items():
        if period == "all":
            score = data.get("total", 0)
        else:
            score = data.get("monthly", {}).get(current_month, 0)
        if score > 0:
            entries.append((str(tester_id), score))
    entries.sort(key=lambda item: item[1], reverse=True)
    return entries


async def _db_tester_leaderboard(session_factory, period: str) -> list:
    async with db_transaction(session_factory) as session:
        current_month = datetime.now().strftime("%m.%Y")
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
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> None:
    """Ruční připsání historických testů (/addtest) — JSON i DB režim."""
    if session_factory is not None:
        return await _db_credit_tester(session_factory, tester_id, amount, month)

    async def _run(tx):
        stats_db = tx.get("testers_stats.json", {})
        stat = stats_db.setdefault(
            tester_id,
            {"total": 0, "lastTested": "", "kits": {}, "tiers": {}, "monthly": {}, "hourlyLogs": []},
        )
        stat["total"] = stat.get("total", 0) + amount
        stat["monthly"][month] = stat["monthly"].get(month, 0) + amount
        tx.set("testers_stats.json", stats_db)

    return await transaction(("testers_stats.json",), _run)


async def remove_tester_credit(
    tester_id: str,
    amount: int,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> int:
    """Odečte ``amount`` z aktuálního měsíce i celkového součtu (/removetest).

    Vrací nový celkový počet testů testera (pro hlášku bota).
    """
    if session_factory is not None:
        return await _db_remove_tester_credit(session_factory, tester_id, amount)

    updated_total = 0

    async def _run(tx):
        nonlocal updated_total
        stats_db = tx.get("testers_stats.json", {})
        stat = stats_db.get(tester_id)
        if not stat:
            stat = {
                "total": 0,
                "lastTested": "",
                "kits": {},
                "tiers": {},
                "monthly": {},
                "hourlyLogs": [],
            }
            stats_db[tester_id] = stat
        stat["total"] = max(0, stat.get("total", 0) - amount)
        stat["monthly"][month_key()] = max(
            0, stat["monthly"].get(month_key(), 0) - amount
        )
        updated_total = stat["total"]
        tx.set("testers_stats.json", stats_db)

    await transaction(("testers_stats.json",), _run)
    return updated_total


async def _db_remove_tester_credit(session_factory, tester_id: str, amount: int) -> int:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(tester_id))
        if player is None:
            return 0
        current = datetime.now().strftime("%m.%Y")
        result = await session.execute(
            select(TesterCredit).where(
                TesterCredit.tester_id == player.id,
                TesterCredit.month == current,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            new_amount = max(0, row.amount - amount)
            await session.execute(
                update(TesterCredit)
                .where(TesterCredit.id == row.id)
                .values(amount=new_amount)
            )
            row.amount = new_amount
        return await _count_stat_results(session, player.id) + (
            await TesterCreditRepository().totals(session, player.id)
        )["total"]


async def _count_stat_results(session, player_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Result)
        .where(
            Result.evaluator_id == player_id,
            Result.kind.in_(RESULT_STAT_KINDS),
        )
    )
    return int(result.scalar_one())


async def _db_credit_tester(session_factory, tester_id: str, amount: int, month: str) -> None:
    async def _once(session):
        player = await PlayerRepository().get_or_create_by_discord_id(
            session, discord_id=int(tester_id), ign=""
        )
        await TesterCreditRepository().credit(
            session, tester_id=player.id, month=month, amount=amount
        )

    try:
        async with db_transaction(session_factory) as session:
            await _once(session)
    except IntegrityError:
        async with db_transaction(session_factory) as session:
            await _once(session)
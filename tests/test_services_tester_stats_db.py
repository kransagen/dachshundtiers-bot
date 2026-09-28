"""services/tester_stats.py — PostgreSQL only (Phase F todo #4).

Statistiky se odvozují za běhu z ``Result`` (jen ``kind`` ticket/queue —
ht_fight se nikdy nepočítal, ani v bývalém JSONu) + ruční kredity
``/addtest`` (``tester_credits``) pro total/monthly.

JSON režim je pryč. ``testers_stats.json`` už není čtený ani zapisovaný;
``test_json_mode_*`` je nahrazen testem, který hlídá, že se k souboru
nesáhne (viz ``test_no_json_mode_anymore``).
"""

import json
from datetime import datetime, timezone
from itertools import count

import pytest
from sqlalchemy import select

from db.models import Result
from db.models.tester_credits import TesterCredit as TesterCreditModel
from db.models.players import TierDefinition
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from services import tester_stats as stats
from services.tester_stats import PRAGUE

NOW_MS = 1_700_000_000_000  # 2023-11-14 (Prague: 14.11.2023, "11.2023")
MARCH_MS = 1_740_789_600_000  # 2025-03-01 (Prague: 01.03.2025, "03.2025")
_KEY = count(1)

KITS = (("ht3", "HT3"), ("tourney", "Tournament"))
TIERS = (
    ("LT5", "ladder", "LT5", 1),
    ("LT4", "ladder", "LT4", 3),
    ("LT3", "ladder", "LT3", 5),
)


def _dt(now_ms: int) -> datetime:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)


async def _seed(session_factory):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        await PlayerRepository().claim_discord_id(
            session, discord_id=100, ign="Owner100"
        )
        await PlayerRepository().claim_discord_id(
            session, discord_id=200, ign="Tester"
        )
        await session.flush()


async def _insert(
    session_factory,
    *,
    player_id: int,
    tester_id: int,
    kind: str,
    kit_key: str = "ht3",
    tier_name: str = "LT4",
    eval_flag: bool = False,
    at: int = NOW_MS,
):
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, kit_key)
        tier = await session.execute(
            select(TierDefinition).where(
                TierDefinition.display_name == tier_name
            )
        )
        tier_row = tier.scalar_one_or_none()
        row = Result(
            result_key=f"stats-{next(_KEY)}-{kind}-{at}-{player_id}",
            kind=kind,
            subtype=None,
            player_id=player_id,
            evaluator_id=tester_id,
            kit_id=kit.id,
            new_tier_id=tier_row.id if tier_row is not None else None,
            eval_flag=eval_flag,
            score="3:1",
            outcome="Won",
            date=_dt(at).astimezone(PRAGUE).strftime("%d.%m.%Y"),
            recorded_at=_dt(at),
        )
        session.add(row)
        await session.flush()
        return row.id


async def _player_id(session_factory, discord_id: int) -> int:
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, discord_id)
        assert player is not None
        return player.id


async def test_tester_stats_derives_from_results_without_ht_fight(
    session_factory, clean_db
):
    await _seed(session_factory)
    ply = await _player_id(session_factory, 100)
    tes = await _player_id(session_factory, 200)
    for kind in ("ticket", "ticket", "queue"):
        await _insert(session_factory, player_id=ply, tester_id=tes, kind=kind)
    await _insert(
        session_factory,
        player_id=ply,
        tester_id=tes,
        kind="ticket",
        tier_name="LT3",
        eval_flag=True,
        at=MARCH_MS,
    )
    await _insert(
        session_factory, player_id=ply, tester_id=tes, kind="ht_fight"
    )

    data = await stats.tester_stats("200", session_factory=session_factory)
    assert data["total"] == 4  # ht_fight se nepočítá
    assert data["kits"] == {"HT3": 4}
    assert data["tiers"] == {"LT4": 3, "LT3 + eval": 1}
    assert data["monthly"] == {"11.2023": 3, "03.2025": 1}
    assert data["lastTested"] == "01.03.2025"
    assert sorted(data["hourlyLogs"]) == [1, 23, 23, 23]


async def test_tester_stats_unknown_tester_none(session_factory, clean_db):
    await _seed(session_factory)
    data = await stats.tester_stats("999", session_factory=session_factory)
    assert data is None


async def test_tester_stats_credits_merge(session_factory, clean_db):
    await _seed(session_factory)
    tes = await _player_id(session_factory, 200)
    await _insert(session_factory, player_id=await _player_id(session_factory, 100), tester_id=tes, kind="queue")
    await stats.credit_tester("200", 5, "03.2025", session_factory=session_factory)
    await stats.credit_tester("200", 2, "11.2023", session_factory=session_factory)

    data = await stats.tester_stats("200", session_factory=session_factory)
    assert data["total"] == 8  # 1 výsledek + 7 kreditů
    assert data["monthly"] == {"11.2023": 3, "03.2025": 5}
    assert data["kits"] == {"HT3": 1}
    assert data["lastTested"] == "14.11.2023"


async def test_tester_stats_credits_only(session_factory, clean_db):
    await _seed(session_factory)
    await stats.credit_tester("200", 3, "01.2025", session_factory=session_factory)
    data = await stats.tester_stats("200", session_factory=session_factory)
    assert data is not None
    assert data["total"] == 3
    assert data["monthly"] == {"01.2025": 3}
    assert data["kits"] == {}
    assert data["tiers"] == {}
    assert data["lastTested"] == ""
    assert data["hourlyLogs"] == []


async def test_credit_tester_accumulates_same_month(session_factory, clean_db):
    await _seed(session_factory)
    await stats.credit_tester("200", 2, "06.2025", session_factory=session_factory)
    await stats.credit_tester("200", 3, "06.2025", session_factory=session_factory)
    async with transaction(session_factory) as session:
        rows = list(
            (await session.execute(select(TesterCreditModel))).scalars()
        )
        assert len(rows) == 1
        assert rows[0].amount == 5
    data = await stats.tester_stats("200", session_factory=session_factory)
    assert data["total"] == 5
    assert data["monthly"] == {"06.2025": 5}


async def test_credit_tester_unknown_user_creates_player(session_factory, clean_db):
    await _seed(session_factory)
    await stats.credit_tester("777", 4, "02.2025", session_factory=session_factory)
    data = await stats.tester_stats("777", session_factory=session_factory)
    assert data is not None
    assert data["total"] == 4
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 777)
        assert player is not None


async def test_tester_leaderboard_all(session_factory, clean_db):
    await _seed(session_factory)
    ply = await _player_id(session_factory, 100)
    tes = await _player_id(session_factory, 200)
    # tester 200: 3 výsledky
    for kind in ("ticket", "queue", "queue"):
        await _insert(session_factory, player_id=ply, tester_id=tes, kind=kind)
    await stats.credit_tester("200", 5, "03.2025", session_factory=session_factory)
    # tester 201: 2 výsledky
    async with transaction(session_factory) as session:
        await PlayerRepository().claim_discord_id(
            session, discord_id=201, ign="Tester2"
        )
        await session.flush()
    tes2 = await _player_id(session_factory, 201)
    await _insert(session_factory, player_id=ply, tester_id=tes2, kind="queue")
    await _insert(session_factory, player_id=ply, tester_id=tes2, kind="queue")

    entries = await stats.tester_leaderboard(
        "all", session_factory=session_factory
    )
    assert entries == [("200", 8), ("201", 2)]


async def test_tester_leaderboard_current_month(session_factory, clean_db):
    await _seed(session_factory)
    ply = await _player_id(session_factory, 100)
    tes = await _player_id(session_factory, 200)
    now_ms = int(datetime.now().timestamp() * 1000)
    await _insert(session_factory, player_id=ply, tester_id=tes, kind="queue", at=now_ms)
    await _insert(session_factory, player_id=ply, tester_id=tes, kind="queue", at=NOW_MS)
    current = datetime.now().strftime("%m.%Y")
    await stats.credit_tester("200", 2, current, session_factory=session_factory)
    await stats.credit_tester("200", 9, "01.2019", session_factory=session_factory)

    entries = await stats.tester_leaderboard(
        "current", session_factory=session_factory
    )
    assert entries == [("200", 3)]  # 1 dnešní výsledek + 2 kredity tento měsíc


async def test_remove_tester_credit(session_factory, clean_db):
    await _seed(session_factory)
    tes = await _player_id(session_factory, 200)
    await _insert(session_factory, player_id=await _player_id(session_factory, 100), tester_id=tes, kind="queue")
    current = datetime.now().strftime("%m.%Y")
    await stats.credit_tester("200", 10, current, session_factory=session_factory)

    total = await stats.remove_tester_credit(
        "200", 4, session_factory=session_factory
    )
    assert total == 7  # 1 výsledek + 6 zbývajících kreditů

    total = await stats.remove_tester_credit(
        "200", 100, session_factory=session_factory
    )
    assert total == 1  # kredity na nule, jen výsledek
    async with transaction(session_factory) as session:
        row = (
            await session.execute(select(TesterCreditModel))
        ).scalar_one()
        assert row.amount == 0


async def test_no_json_mode_anymore(session_factory, clean_db, tmp_path, monkeypatch):
    """``testers_stats.json`` is gone: no silent second source of truth.

    A statistics function that can answer from a file when the database is not
    reachable is exactly the failure mode this cleanup removes — the two would
    drift, and nobody could tell which one a number came from. Every entry
    point must therefore refuse rather than fall back.
    """
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    (tmp_path / "testers_stats.json").write_text(
        '{"999": {"total": 3, "monthly": {"04.2025": 3}}}'
    )

    for call in (
        stats.tester_stats("999", session_factory=None),
        stats.tester_leaderboard("all", session_factory=None),
        stats.credit_tester("999", 3, "04.2025", session_factory=None),
        stats.remove_tester_credit("999", 1, session_factory=None),
    ):
        with pytest.raises(RuntimeError, match="PostgreSQL"):
            await call

    # The file was neither read nor written.
    assert json.loads((tmp_path / "testers_stats.json").read_text()) == {
        "999": {"total": 3, "monthly": {"04.2025": 3}}
    }


async def test_stats_refuse_without_a_session_factory():
    """A missing argument is a TypeError, not a fallback to a file."""
    with pytest.raises(TypeError):
        await stats.tester_stats("999")
    with pytest.raises(TypeError):
        await stats.tester_leaderboard("all")
    with pytest.raises(TypeError):
        await stats.credit_tester("999", 1, "04.2025")
    with pytest.raises(TypeError):
        await stats.remove_tester_credit("999", 1)
"""/retire a automatické peak tiery – prahy, výhry, monotónnost peaku."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db.models import Kit, Player, PlayerCurrentTier, PlayerPeakTier, TierDefinition
from db.repositories.kits import ensure_dimensions
from db.repositories.results import ResultRepository
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction
from db.services.retire_peak import (
    Progress,
    RetireRefused,
    commit_retire,
    player_peaks,
    player_progress,
    plan_retire,
    sweep_peaks,
)

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
TIERS = (
    ("HT3", "ladder", "HT3", 6), ("LT2", "ladder", "LT2", 7), ("HT2", "ladder", "HT2", 8),
    ("LT1", "ladder", "LT1", 9), ("HT1", "ladder", "HT1", 10),
    ("LT3", "ladder", "LT3", 5),
)


async def _seed(session_factory, tier="HT2", days=61, discord_id=1, ign="Alice"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, (("nethpot", "NethPot"),), TIERS)
        kit = (await session.execute(select(Kit))).scalar_one()
        player = Player(discord_id=discord_id, ign=ign, ign_linked_at=NOW)
        session.add(player)
        await session.flush()
        tid = (await session.execute(
            select(TierDefinition.id).where(TierDefinition.code == tier))).scalar_one()
        await MirrorServiceRepository().apply_observation(
            session, player_id=player.id, kit_id=kit.id, tier_id=tid,
            observed_at=NOW - timedelta(days=days), source="discord_sync",
        )
        return player.id, kit.id


async def _tier_id(session, code):
    return (await session.execute(
        select(TierDefinition.id).where(TierDefinition.code == code))).scalar_one()


async def _win(session_factory, player_id, kit_id, testee_tier, key, kind="ht_fight"):
    async with transaction(session_factory) as session:
        tester = Player(ign=f"T{key}")
        session.add(tester)
        await session.flush()
        await ResultRepository().insert(
            session, result_key=key, kind=kind, player_id=tester.id, kit_id=kit_id,
            evaluator_id=player_id if kind == "ticket" else None,
            opponent_id=1 if kind == "ht_fight" else None,
            previous_tier_id=await _tier_id(session, testee_tier),
            outcome="Won" if kind == "ticket" else "Lost", recorded_at=datetime.now(timezone.utc) - timedelta(days=1),
        )


def test_progress_thresholds():
    p = Progress(1, "K", "LT1", 1, NOW, 89, 2, 90, 3)
    assert not p.eligible and p.days_left == 1 and p.wins_left == 1
    assert Progress(1, "K", "LT1", 1, NOW, 90, 0, 90, 3).reason == "days"
    assert Progress(1, "K", "LT1", 1, NOW, 0, 3, 90, 3).reason == "wins"
    ht3 = Progress(1, "K", "HT3", 1, NOW, 10, 99, 60, None)
    assert not ht3.eligible and ht3.wins_left is None


async def test_retire_refused_before_threshold(session_factory, clean_db):
    await _seed(session_factory, tier="HT2", days=10)
    with pytest.raises(RetireRefused) as err:
        await plan_retire(session_factory, 1, "NethPot", now=NOW)
    assert "50 dní" in str(err.value) and "2 výher" in str(err.value)


async def test_retire_refused_for_low_tier_and_unlinked(session_factory, clean_db):
    await _seed(session_factory, tier="HT3", days=200)
    with pytest.raises(RetireRefused):
        await plan_retire(session_factory, 1, "NethPot", now=NOW)
    with pytest.raises(RetireRefused):
        await plan_retire(session_factory, 999, "NethPot", now=NOW)


async def test_retire_by_days_writes_retired_tier_and_peak(session_factory, clean_db):
    player_id, kit_id = await _seed(session_factory, tier="LT2", days=61)
    plan = await plan_retire(session_factory, 1, "nethpot", now=NOW)
    assert plan.retired_tier_code == "RLT2"
    await commit_retire(session_factory, plan, actor_name="Alice")
    async with transaction(session_factory) as session:
        mirror = (await session.execute(select(PlayerCurrentTier))).scalar_one()
        assert (await session.get(TierDefinition, mirror.tier_id)).code == "RLT2"
        assert mirror.source == "retire"
        assert await player_peaks(session, await session.get(Player, player_id)) == [
            ("NethPot", "LT2")]
    with pytest.raises(RetireRefused):
        await plan_retire(session_factory, 1, "NethPot", now=NOW)


async def test_lt1_needs_90_days(session_factory, clean_db):
    await _seed(session_factory, tier="LT1", days=80)
    with pytest.raises(RetireRefused):
        await plan_retire(session_factory, 1, "NethPot", now=NOW)


async def test_wins_only_count_same_or_one_rank_below(session_factory, clean_db):
    player_id, kit_id = await _seed(session_factory, tier="HT2", days=5)
    await _win(session_factory, player_id, kit_id, "LT3", "w0")  # o dva níž – nepočítá
    await _win(session_factory, player_id, kit_id, "LT2", "w1")
    await _win(session_factory, player_id, kit_id, "LT2", "w1t", kind="ticket")  # běžný ticket se nepočítá
    async with transaction(session_factory) as session:
        (prog,) = await player_progress(session, await session.get(Player, player_id), now=NOW)
    assert prog.wins == 1 and not prog.eligible
    await _win(session_factory, player_id, kit_id, "HT2", "w2")
    plan = await plan_retire(session_factory, 1, "NethPot", now=NOW)
    assert plan.progress.reason == "wins"


async def test_sweep_grants_ht3_peak_and_never_lowers(session_factory, clean_db):
    player_id, kit_id = await _seed(session_factory, tier="HT3", days=61)
    assert await sweep_peaks(session_factory) == 1
    assert await sweep_peaks(session_factory) == 0
    async with transaction(session_factory) as session:
        tier = await _tier_id(session, "LT2")
        await MirrorServiceRepository().apply_observation(
            session, player_id=player_id, kit_id=kit_id, tier_id=tier,
            observed_at=NOW - timedelta(days=61) + timedelta(days=1), source="promotion",
        )
        await MirrorServiceRepository().apply_observation(
            session, player_id=player_id, kit_id=kit_id, tier_id=await _tier_id(session, "HT3"),
            observed_at=datetime.now(timezone.utc) - timedelta(days=100), source="manual",
        )
    async with transaction(session_factory) as session:
        peak = (await session.execute(select(PlayerPeakTier))).scalar_one()
        assert (await session.get(TierDefinition, peak.tier_id)).code == "HT3"


async def test_commit_retire_refused_when_tier_changed_since_plan(session_factory, clean_db):
    player_id, kit_id = await _seed(session_factory, tier="LT2", days=61)
    plan = await plan_retire(session_factory, 1, "nethpot")
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session, player_id=player_id, kit_id=kit_id,
            tier_id=await _tier_id(session, "HT2"),
            observed_at=datetime.now(timezone.utc), source="promotion",
        )
    with pytest.raises(RetireRefused):
        await commit_retire(session_factory, plan, actor_name="Alice")
    async with transaction(session_factory) as session:
        mirror = (await session.execute(select(PlayerCurrentTier))).scalar_one()
        assert (await session.get(TierDefinition, mirror.tier_id)).code == "HT2"
        assert await player_peaks(session, await session.get(Player, player_id)) == []

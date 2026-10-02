"""H3 audit fix regression: ``_rollback_conflicts_with_later_promotion``.

``/sync rollback`` replays ``playersync_log.json`` (scoped to
``/sync discord apply``) and has no way to know about a later, unrelated
``auto_grant_kit_role`` promotion for the same member+kit. The guard in
``cogs/_shared.py`` closes that gap by checking real PostgreSQL tier
history for a ``source="promotion"`` entry after the sync being rolled
back, and this was previously untested. Covers cases A-E from the Phase H
continuation brief.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from cogs._shared import _rollback_conflicts_with_later_promotion, apply_rollback_actions
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction

KITS = (("ht3", "HT3"),)
TIERS = (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2))


async def _seed_kit(session):
    await ensure_dimensions(session, KITS, TIERS)
    await session.flush()
    return await KitRepository().get_by_key(session, "ht3")


async def _seed_player(session, discord_id: int, ign: str):
    _, player = await PlayerRepository().claim_discord_id(
        session, discord_id=discord_id, ign=ign
    )
    return player


async def _observe(session_factory, *, player_id, kit_id, tier_code, at, source):
    from db.repositories.kits import TierDefinitionRepository

    async with transaction(session_factory) as session:
        tier = await TierDefinitionRepository().get_by_code(session, tier_code)
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=player_id,
            kit_id=kit_id,
            tier_id=tier.id,
            observed_at=at,
            source=source,
        )


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _member(mid, role_ids=()):
    m = SimpleNamespace(
        id=int(mid),
        roles=[SimpleNamespace(id=int(r)) for r in role_ids],
    )
    m.add_roles = mock.AsyncMock()
    m.remove_roles = mock.AsyncMock()
    return m


def _guild(members):
    by_id = {int(m.id): m for m in members}
    g = mock.MagicMock()
    g.members = list(members)
    g.get_member.side_effect = lambda mid: by_id.get(mid)
    g.fetch_member = mock.AsyncMock(side_effect=lambda mid: by_id.get(mid))
    g.get_role.side_effect = lambda rid: SimpleNamespace(id=int(rid))
    return g


def _rollback_action(op, role_id, *, member_id):
    return {
        "op": op,
        "original_op": "remove" if op == "add" else "add",
        "member_id": str(member_id),
        "member_name": "AliceMC",
        "role_id": str(role_id),
        "kit": "ht3",
        "tier": "T1",
    }


NOW = datetime.now(timezone.utc)
TARGET_TS = _ts_ms(NOW)


# ---------------------------------------------------------------------------
# Case A: no newer promotion -> rollback allowed.
# ---------------------------------------------------------------------------
async def test_case_a_no_newer_promotion_allows_rollback(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_kit(session)
        player = await _seed_player(session, 1001, "Alice")
    await _observe(
        session_factory,
        player_id=player.id,
        kit_id=kit.id,
        tier_code="t1",
        at=NOW - timedelta(seconds=5),
        source="discord_sync",
    )

    conflict = await _rollback_conflicts_with_later_promotion(
        session_factory, discord_id=1001, kit_key="ht3", target_ts=TARGET_TS
    )
    assert conflict is None


# ---------------------------------------------------------------------------
# Case B: a newer promotion for the same member+kit -> rollback skipped.
# ---------------------------------------------------------------------------
async def test_case_b_newer_promotion_same_member_kit_blocks_rollback(
    session_factory, clean_db
):
    async with transaction(session_factory) as session:
        kit = await _seed_kit(session)
        player = await _seed_player(session, 1002, "Bob")
    await _observe(
        session_factory,
        player_id=player.id,
        kit_id=kit.id,
        tier_code="t2",
        at=NOW + timedelta(seconds=5),
        source="promotion",
    )

    conflict = await _rollback_conflicts_with_later_promotion(
        session_factory, discord_id=1002, kit_key="ht3", target_ts=TARGET_TS
    )
    assert conflict is not None
    assert "znovu povýšen" in conflict


# ---------------------------------------------------------------------------
# Case C: multiple later promotions -> still blocked.
# ---------------------------------------------------------------------------
async def test_case_c_multiple_later_promotions_still_blocks(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_kit(session)
        player = await _seed_player(session, 1003, "Carol")
    await _observe(
        session_factory,
        player_id=player.id,
        kit_id=kit.id,
        tier_code="t1",
        at=NOW + timedelta(seconds=5),
        source="promotion",
    )
    await _observe(
        session_factory,
        player_id=player.id,
        kit_id=kit.id,
        tier_code="t2",
        at=NOW + timedelta(seconds=10),
        source="promotion",
    )

    conflict = await _rollback_conflicts_with_later_promotion(
        session_factory, discord_id=1003, kit_key="ht3", target_ts=TARGET_TS
    )
    assert conflict is not None


# ---------------------------------------------------------------------------
# Case D: an unrelated player/kit has a later promotion -> does not block.
# ---------------------------------------------------------------------------
async def test_case_d_unrelated_player_later_promotion_does_not_block(
    session_factory, clean_db
):
    async with transaction(session_factory) as session:
        kit = await _seed_kit(session)
        await _seed_player(session, 1004, "Dave")
        other = await _seed_player(session, 1005, "Eve")
    await _observe(
        session_factory,
        player_id=other.id,
        kit_id=kit.id,
        tier_code="t2",
        at=NOW + timedelta(seconds=5),
        source="promotion",
    )

    conflict = await _rollback_conflicts_with_later_promotion(
        session_factory, discord_id=1004, kit_key="ht3", target_ts=TARGET_TS
    )
    assert conflict is None


# ---------------------------------------------------------------------------
# Case E: audit target exists but the only later change is a non-promotion
# mirror refresh (e.g. discord_sync re-observation) -> existing safety
# behaviour is preserved: the guard only cares about source="promotion",
# so rollback is still allowed.
# ---------------------------------------------------------------------------
async def test_case_e_later_non_promotion_change_does_not_block(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_kit(session)
        player = await _seed_player(session, 1006, "Frank")
    await _observe(
        session_factory,
        player_id=player.id,
        kit_id=kit.id,
        tier_code="t2",
        at=NOW + timedelta(seconds=5),
        source="discord_sync",
    )

    conflict = await _rollback_conflicts_with_later_promotion(
        session_factory, discord_id=1006, kit_key="ht3", target_ts=TARGET_TS
    )
    assert conflict is None


# ---------------------------------------------------------------------------
# Integration: apply_rollback_actions surfaces skipped_newer_promotion.
# ---------------------------------------------------------------------------
async def test_apply_rollback_actions_skips_on_newer_promotion(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_kit(session)
        player = await _seed_player(session, 1007, "Grace")
    await _observe(
        session_factory,
        player_id=player.id,
        kit_id=kit.id,
        tier_code="t2",
        at=NOW + timedelta(seconds=5),
        source="promotion",
    )

    alice = _member(1007, role_ids=(101,))
    guild = _guild([alice])
    actions = [_rollback_action("remove", 101, member_id=1007)]

    results = await apply_rollback_actions(
        guild, actions, session_factory=session_factory, target_ts=TARGET_TS
    )
    assert results[0]["status"] == "skipped_newer_promotion"
    alice.add_roles.assert_not_awaited()
    alice.remove_roles.assert_not_awaited()


async def test_apply_rollback_actions_applies_without_newer_promotion(
    session_factory, clean_db
):
    async with transaction(session_factory) as session:
        await _seed_kit(session)
        await _seed_player(session, 1008, "Heidi")

    alice = _member(1008, role_ids=(101,))
    guild = _guild([alice])
    actions = [_rollback_action("remove", 101, member_id=1008)]

    results = await apply_rollback_actions(
        guild, actions, session_factory=session_factory, target_ts=TARGET_TS
    )
    assert results[0]["status"] == "applied"
    alice.remove_roles.assert_awaited_once()

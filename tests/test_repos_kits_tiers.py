"""Kit, tier-definition, kit-role and mirror/history repository tests."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from db.models import PlayerCurrentTier
from db.repositories.kits import (
    KitRepository,
    KitRoleRepository,
    TierDefinitionRepository,
    ensure_dimensions,
)
from db.repositories.tiers import (
    MirrorRepository,
    MirrorServiceRepository,
    TierHistoryRepository,
)
from db.services.session import transaction

KITS = (("ht3", "HT3"), ("tourney", "Tournament"))
TIERS = (
    ("t1", "ladder", "Tier 1", 1),
    ("t2", "ladder", "Tier 2", 2),
    ("t3", "ladder", "Tier 3", 3),
)


@pytest.fixture
def player_repo():
    from db.repositories.players import PlayerRepository

    return PlayerRepository()


async def _seed_dimensions(session):
    await ensure_dimensions(session, KITS, TIERS)
    await session.flush()
    kit = await KitRepository().get_by_key(session, "ht3")
    t2 = await TierDefinitionRepository().get_by_code(session, "t2")
    t3 = await TierDefinitionRepository().get_by_code(session, "t3")
    return kit, t2, t3


async def _seed_player(session, repo, discord_id: int, ign: str):
    _, player = await repo.claim_discord_id(session, discord_id=discord_id, ign=ign)
    return player


async def test_dimensions_seed_is_idempotent(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        kits = await KitRepository().list(session)
        tiers = await TierDefinitionRepository().list(session)
    assert len(kits) == 2
    assert len(tiers) == 3


async def test_kit_role_set_mapping_upserts_single_row(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit, t2, t3 = await _seed_dimensions(session)
        repo = KitRoleRepository()
        await repo.set_mapping(session, kit_id=kit.id, tier_id=t2.id, discord_role_id=777001)
        twice = await repo.set_mapping(session, kit_id=kit.id, tier_id=t2.id, discord_role_id=777001)
        rebound = await repo.set_mapping(session, kit_id=kit.id, tier_id=t2.id, discord_role_id=777002)
        roles = await repo.get_all(session)
    assert len(roles) == 1
    assert roles[0].discord_role_id == 777002
    assert twice.id == roles[0].id
    assert rebound.id == roles[0].id
    async with transaction(session_factory) as session:
        kit, _, t3 = await _seed_dimensions(session)
        repo = KitRoleRepository()
        await repo.remove_mapping(session, discord_role_id=777002)
        remapped = await repo.set_mapping(session, kit_id=kit.id, tier_id=t3.id, discord_role_id=777002)
        one = await repo.get_by_role(session, 777002)
    assert one is not None and one.tier_id == t3.id
    assert remapped.id == one.id


async def test_kit_role_duplicate_role_rejected(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit, t2, t3 = await _seed_dimensions(session)
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t2.id, discord_role_id=999001
        )
        with pytest.raises(IntegrityError):
            await KitRoleRepository().set_mapping(
                session, kit_id=kit.id, tier_id=t3.id, discord_role_id=999001
            )


async def test_mirror_first_observation_is_origin_without_previous(
    session_factory, clean_db, player_repo
):
    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        kit, t2, _ = await _seed_dimensions(session)
        player = await _seed_player(session, player_repo, 100, "Mirror1")
        result = await MirrorServiceRepository().apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=t2.id,
            observed_at=now,
            source="discord_sync",
        )
    assert result.tier_changed is True
    assert result.first_observation is True
    assert result.previous_tier_id is None
    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=player.id, kit_id=kit.id
        )
        history = await TierHistoryRepository().list_for_player(
            session, player_id=player.id
        )
    assert mirror is not None and mirror.tier_id == t2.id
    assert len(history) == 1
    assert history[0].previous_tier_id is None


async def test_mirror_same_tier_refreshes_without_history(
    session_factory, clean_db, player_repo
):
    later = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        kit, t2, _ = await _seed_dimensions(session)
        player = await _seed_player(session, player_repo, 101, "Mirror2")
        svc = MirrorServiceRepository()
        await svc.apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=t2.id,
            observed_at=later,
            source="discord_sync",
        )
        result = await svc.apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=t2.id,
            observed_at=later,
            source="discord_sync",
        )
    assert result.tier_changed is False
    assert result.history_id is None
    async with transaction(session_factory) as session:
        history = await TierHistoryRepository().list_for_player(
            session, player_id=player.id
        )
    assert len(history) == 1


async def test_mirror_transition_appends_history_with_previous(
    session_factory, clean_db, player_repo
):
    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        kit, t2, t3 = await _seed_dimensions(session)
        player = await _seed_player(session, player_repo, 102, "Mirror3")
        svc = MirrorServiceRepository()
        await svc.apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=t2.id,
            observed_at=now,
            source="discord_sync",
        )
        result = await svc.apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=t3.id,
            observed_at=now + timedelta(seconds=1),
            source="promotion",
        )
    assert result.tier_changed is True
    assert result.first_observation is False
    assert result.previous_tier_id == t2.id
    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=player.id, kit_id=kit.id
        )
        history = await TierHistoryRepository().list_for_player(
            session, player_id=player.id
        )
    assert mirror is not None and mirror.tier_id == t3.id
    assert [h.tier_id for h in history] == [t3.id, t2.id]
    assert history[0].previous_tier_id == t2.id


async def test_mirror_and_history_are_append_only(session_factory, clean_db):
    assert not hasattr(MirrorRepository(), "unobserve")
    assert not hasattr(TierHistoryRepository(), "update")
    assert not hasattr(TierHistoryRepository(), "delete")
    assert not hasattr(MirrorServiceRepository(), "clear")


async def test_mirror_reject_unknown_source(session_factory, clean_db, player_repo):
    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        kit, t2, _ = await _seed_dimensions(session)
        player = await _seed_player(session, player_repo, 300, "Src")
    async with session_factory() as session:
        session.add(PlayerCurrentTier(
            player_id=player.id, kit_id=kit.id, tier_id=t2.id,
            observed_at=now, source="hacked"
        ))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_kit_role_removal_and_snapshot(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit, t2, _ = await _seed_dimensions(session)
        repo = KitRoleRepository()
        await repo.set_mapping(session, kit_id=kit.id, tier_id=t2.id, discord_role_id=55001)
        snapshot = await repo.role_snapshot(session)
        removed = await repo.remove_mapping(session, discord_role_id=55001)
    assert snapshot == {55001: (kit.id, t2.id)}
    assert removed == 1
    async with transaction(session_factory) as session:
        assert await KitRoleRepository().get_all(session) == []
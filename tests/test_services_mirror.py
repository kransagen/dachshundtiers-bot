"""Mirror classification (pure) + MirrorService anomaly-safety tests."""

from datetime import datetime, timezone

from db.repositories.kits import ensure_dimensions
from db.repositories.tiers import MirrorRepository
from db.services.session import transaction
from db.services.tier_mirror import (
    ANOMALY_MISSING_TIER,
    ANOMALY_MULTIPLE_TIERS,
    MirrorService,
    classify_member_roles,
)

_ROLE_MAP = {101: (1, 11), 102: (1, 12), 201: (2, 21)}
_KIT_IDS = {"ht3": 1, "tourney": 2}
_KIT_KEYS = {1: "ht3", 2: "tourney"}


def test_classify_clean_observation():
    result = classify_member_roles(_ROLE_MAP, {101}, _KIT_IDS, _KIT_KEYS)
    clean = [o for o in result.observations if o.anomaly is None]
    assert len(clean) == 1
    assert clean[0].kit_id == 1
    assert clean[0].tier_id == 11
    assert clean[0].discord_role_id == 101
    assert result.unknown_role_ids == ()


def test_classify_multiple_roles_is_anomaly_not_observation():
    result = classify_member_roles(_ROLE_MAP, {101, 102}, _KIT_IDS, _KIT_KEYS)
    ht3 = [o for o in result.observations if o.kit_id == 1][0]
    assert ht3.anomaly == ANOMALY_MULTIPLE_TIERS
    assert ht3.tier_id is None


def test_classify_missing_tier_flagged_per_kit():
    result = classify_member_roles(_ROLE_MAP, {201}, _KIT_IDS, _KIT_KEYS)
    anomalies = [o for o in result.observations if o.anomaly]
    assert {o.kit_key for o in anomalies} == {"ht3"}
    assert anomalies[0].anomaly == ANOMALY_MISSING_TIER
    assert anomalies[0].tier_id is None


def test_classify_unknown_roles_reported_separately():
    result = classify_member_roles(_ROLE_MAP, {101, 999}, _KIT_IDS, _KIT_KEYS)
    assert result.unknown_role_ids == (999,)


def test_classify_is_pure_and_deterministic():
    first = classify_member_roles(_ROLE_MAP, {101, 201}, _KIT_IDS, _KIT_KEYS)
    second = classify_member_roles(_ROLE_MAP, {101, 201}, _KIT_IDS, _KIT_KEYS)
    assert first == second
    assert sorted(o.kit_id for o in first.observations) == [1, 2]


async def test_mirror_service_never_applies_anomalies(session_factory, clean_db):
    from sqlalchemy import select

    from db.models import Kit
    from db.repositories.players import PlayerRepository

    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=900, ign="Anomaly"
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        classification = classify_member_roles(
            {700: (kit.id, 1)}, {999, 998}, {"ht3": kit.id}, {kit.id: "ht3"}
        )
        applied = await MirrorService().apply_observations(
            session,
            player_id=player.id,
            classification=classification,
            observed_at=now,
            source="discord_sync",
        )
        mirror_rows = await MirrorRepository().list_current(
            session, player_id=player.id
        )
    assert applied == []
    assert mirror_rows == []
    assert classification.unknown_role_ids == (998, 999)
    assert any(o.anomaly == ANOMALY_MISSING_TIER for o in classification.observations)


async def test_mirror_service_applies_only_clean_observations(session_factory, clean_db):
    from sqlalchemy import select as sa_select

    from db.models import Kit
    from db.repositories.players import PlayerRepository
    from db.repositories.tiers import MirrorRepository

    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=901, ign="Clean"
        )
        kit = (await session.execute(sa_select(Kit).where(Kit.key == "ht3"))).scalar_one()
        classification = classify_member_roles(
            {700: (kit.id, 1)}, {700}, {"ht3": kit.id}, {kit.id: "ht3"}
        )
        applied = await MirrorService().apply_observations(
            session,
            player_id=player.id,
            classification=classification,
            observed_at=now,
            source="discord_sync",
        )
        mirror_rows = await MirrorRepository().list_current(
            session, player_id=player.id
        )
    assert len(applied) == 1
    assert applied[0].tier_changed is True
    assert len(mirror_rows) == 1
    assert mirror_rows[0].tier_id == 1
"""services/cooldowns.py — PostgreSQL only.

Business rule: cooldowns are per player+kit+type. Waitlist and HT3 are both
reported per kit; a pre-migration global waitlist row (kit_id IS NULL) is
reported separately and must never be attributed to one kit.
"""

from datetime import datetime, timedelta, timezone

from db.models import Kit
from db.repositories.cooldowns import COOLDOWN_HT3, COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from services import cooldowns
from sqlalchemy import select

KIT_DEFS = (("molepvp", "MolePVP"), ("boxing", "Boxing"))
TIER_DEFS = (("HT3", "ladder", "HT3", 3),)


async def _seed_player(session_factory, *, discord_id=1111, ign="mendu__"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KIT_DEFS, TIER_DEFS)
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=discord_id, ign=ign
        )
        return player


async def _get_kit(session_factory, key: str) -> Kit:
    async with transaction(session_factory) as session:
        return (
            await session.execute(select(Kit).where(Kit.key == key))
        ).scalar_one()


async def test_db_unknown_player_no_cooldowns(session_factory, clean_db):
    result = await cooldowns.get_cooldowns(9999, session_factory=session_factory)
    assert result == {"waitlist": {}, "ht3": {}, "waitlist_legacy_global_ms": None}


async def test_db_waitlist_cooldown_is_per_kit(session_factory, clean_db):
    player = await _seed_player(session_factory)
    molepvp = await _get_kit(session_factory, "molepvp")
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=molepvp.id,
            expires_at=expires,
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert set(result["waitlist"]) == {"MolePVP"}
    assert result["waitlist"]["MolePVP"] > 0
    assert result["ht3"] == {}
    assert result["waitlist_legacy_global_ms"] is None

    # A different kit has no cooldown at all — never blocked by MolePVP's.
    assert await cooldowns.get_waitlist_cooldown_ms(
        player.discord_id, "boxing", session_factory=session_factory
    ) is None
    assert await cooldowns.get_waitlist_cooldown_ms(
        player.discord_id, "molepvp", session_factory=session_factory
    ) is not None


async def test_db_waitlist_expired_returns_none(session_factory, clean_db):
    player = await _seed_player(session_factory)
    molepvp = await _get_kit(session_factory, "molepvp")
    expires = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=molepvp.id,
            expires_at=expires,
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert result == {"waitlist": {}, "ht3": {}, "waitlist_legacy_global_ms": None}


async def test_db_legacy_global_waitlist_reported_separately_and_blocks_every_kit(
    session_factory, clean_db
):
    player = await _seed_player(session_factory)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=None,
            expires_at=expires,
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert result["waitlist"] == {}
    assert result["waitlist_legacy_global_ms"] is not None

    for kit_key in ("molepvp", "boxing"):
        remaining = await cooldowns.get_waitlist_cooldown_ms(
            player.discord_id, kit_key, session_factory=session_factory
        )
        assert remaining is not None


async def test_db_ht3_cooldowns_keyed_by_kit_name(session_factory, clean_db):
    player = await _seed_player(session_factory)
    kit = await _get_kit(session_factory, "molepvp")
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_HT3,
            kit_id=kit.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=3),
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert result["waitlist"] == {}
    assert set(result["ht3"]) == {"MolePVP"}
    assert result["ht3"]["MolePVP"] > 0


async def test_db_ht3_expired_excluded(session_factory, clean_db):
    player = await _seed_player(session_factory)
    kit = await _get_kit(session_factory, "molepvp")
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_HT3,
            kit_id=kit.id,
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert result == {"waitlist": {}, "ht3": {}, "waitlist_legacy_global_ms": None}

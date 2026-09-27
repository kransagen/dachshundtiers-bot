"""DB (PostgreSQL) i JSON režim services/cooldowns.py — Phase F (todo #10a).

Dual-mode čtečka pro /cooldown: DB režim (CooldownRepository — waitlist
s kit_id NULL + HT3 per kit) a JSON režim (legacy cooldowns.json +
ht3_cooldowns.json, parity beze změny).
"""

import json
import time
from datetime import datetime, timedelta, timezone

from db.repositories.cooldowns import COOLDOWN_HT3, COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from services import cooldowns

KIT_DEFS = (("molepvp", "MolePVP"),)
TIER_DEFS = (("HT3", "ladder", "HT3", 3),)


async def _seed_player(session_factory, *, discord_id=1111, ign="mendu__"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KIT_DEFS, TIER_DEFS)
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=discord_id, ign=ign
        )
        return player


async def test_db_unknown_player_no_cooldowns(session_factory, clean_db):
    result = await cooldowns.get_cooldowns(9999, session_factory=session_factory)
    assert result == {"waitlist_ms": None, "ht3": {}}


async def test_db_waitlist_cooldown_remaining(session_factory, clean_db):
    player = await _seed_player(session_factory)
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            expires_at=expires,
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert result["waitlist_ms"] is not None
    assert result["waitlist_ms"] > 0
    assert result["ht3"] == {}


async def test_db_waitlist_expired_returns_none(session_factory, clean_db):
    player = await _seed_player(session_factory)
    expires = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            expires_at=expires,
        )
    result = await cooldowns.get_cooldowns(
        player.discord_id, session_factory=session_factory
    )
    assert result == {"waitlist_ms": None, "ht3": {}}


async def test_db_ht3_cooldowns_keyed_by_kit_name(session_factory, clean_db):
    player = await _seed_player(session_factory)
    async with transaction(session_factory) as session:
        from db.models import Kit

        kit = (
            await session.execute(
                __import__("sqlalchemy").select(Kit).where(Kit.key == "molepvp")
            )
        ).scalar_one()
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
    assert result["waitlist_ms"] is None
    assert set(result["ht3"]) == {"MolePVP"}
    assert result["ht3"]["MolePVP"] > 0


async def test_db_ht3_expired_excluded(session_factory, clean_db):
    player = await _seed_player(session_factory)
    async with transaction(session_factory) as session:
        from db.models import Kit

        kit = (
            await session.execute(
                __import__("sqlalchemy").select(Kit).where(Kit.key == "molepvp")
            )
        ).scalar_one()
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
    assert result == {"waitlist_ms": None, "ht3": {}}


async def test_json_waitlist_from_last_test_timestamp(tmp_path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    now = int(time.time() * 1000)
    (tmp_path / "cooldowns.json").write_text(
        json.dumps({"1111": now - 60_000}), encoding="utf-8"
    )
    (tmp_path / "ht3_cooldowns.json").write_text(json.dumps({}), encoding="utf-8")
    result = await cooldowns.get_cooldowns(
        "1111", waitlist_cooldown_ms=4 * 24 * 60 * 60 * 1000
    )
    assert result["waitlist_ms"] is not None
    assert result["waitlist_ms"] > 0
    assert result["ht3"] == {}


async def test_json_ht3_cooldowns(tmp_path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    future = int(time.time() * 1000) + 7 * 60 * 60 * 1000
    (tmp_path / "cooldowns.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "ht3_cooldowns.json").write_text(
        json.dumps({"1111": {"MolePVP": future}}), encoding="utf-8"
    )
    t0 = int(time.time() * 1000)
    result = await cooldowns.get_cooldowns("1111")
    t1 = int(time.time() * 1000)
    assert result["waitlist_ms"] is None
    remaining = result["ht3"]["MolePVP"]
    assert future - t1 <= remaining <= future - t0


async def test_json_unknown_player_empty(tmp_path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    result = await cooldowns.get_cooldowns("9999")
    assert result == {"waitlist_ms": None, "ht3": {}}
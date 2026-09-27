"""DB (PostgreSQL) režim services/edituser.py + services/player_export.py — Phase F (#10d).

Krok A: ``apply_player_edit`` / ``execute_player_edit`` běží na PostgreSQL, když
je předán ``session_factory`` (F10 – žádný JSON read; player_after se staví
z DB přes ``build_player_shape``, web canonical přes ``export_players``).
Krok B: canonical JSON tvar hráče z DB (modes/history/discordId) a
``export_players``.

JSON režim (session_factory=None) je pokryt stávajícími tests/test_edituser.py
a zůstává beze změny chování.
"""

import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from db.models import AuditLog, PlayerCurrentTier, TierHistory
from db.repositories.kits import (
    KitRepository,
    TierDefinitionRepository,
    ensure_dimensions,
)
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction
from services import edituser
from services import player_export

KIT_DEFS = (("molepvp", "MolePVP"),)
TIER_DEFS = (
    ("LT3", "ladder", "LT3", 1),
    ("HT3", "ladder", "HT3", 3),
)

NOW = int(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
QUEUE_MS = 4 * 24 * 60 * 60 * 1000
HT3_MS = 7 * 24 * 60 * 60 * 1000

ACTOR_ID = 999888777666555
ACTOR_NAME = "tester-admin"


async def _seed_player(
    session_factory,
    *,
    discord_id=111111111111111111,
    ign="mendu__",
    tier_code="LT3",
):
    """Hráč + kit + ladder tier + mirror observation (aktuální tier na kitu)."""
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KIT_DEFS, TIER_DEFS)
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=discord_id, ign=ign
        )
        kit = await KitRepository().get_by_key(session, "molepvp")
        tier = await TierDefinitionRepository().get_by_code(session, tier_code)
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=tier.id,
            observed_at=datetime.now(timezone.utc) - timedelta(hours=1),
            source="manual",
            reason="test-seed",
        )
        return player


async def _audit_entries(session_factory) -> list[AuditLog]:
    async with transaction(session_factory) as session:
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "edituser").order_by(AuditLog.id)
            )
        ).scalars()
        return list(rows)


# ---------------------------------------------------------------------------
# apply_player_edit – ignor (pole "ign")
# ---------------------------------------------------------------------------
async def test_db_ign_change_applies_and_audits(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "ign", "new_value": "MenduTwo"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "changed"
    assert result["old_value"] == "mendu__"
    assert result["new_value"] == "MenduTwo"
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 111111111111111111)
        assert player.ign == "MenduTwo"
    entries = await _audit_entries(session_factory)
    assert len(entries) == 1
    assert entries[0].details["field"] == "ign"
    assert entries[0].details["newValue"] == "MenduTwo"


async def test_db_ign_case_insensitive_unchanged(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "ign", "new_value": "MENDU__"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "unchanged"
    assert await _audit_entries(session_factory) == []


async def test_db_ign_conflict_rejected(session_factory, clean_db):
    await _seed_player(session_factory)
    await _seed_player(session_factory, discord_id=222222222222222222, ign="bob_")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "ign", "new_value": "bob_"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "patří jinému záznamu" in result["message"]


async def test_db_ign_empty_rejected(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "ign", "new_value": "  "},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# apply_player_edit – Discord ID (pole "discord_id")
# ---------------------------------------------------------------------------
async def test_db_discord_id_change_applies_and_audits(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "discord_id", "new_value": "333333333333333333"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "changed"
    assert result["old_value"] == "111111111111111111"
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 333333333333333333)
        assert player is not None
    entries = await _audit_entries(session_factory)
    assert len(entries) == 1
    assert entries[0].details["field"] == "discord_id"


async def test_db_discord_id_unchanged(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "discord_id", "new_value": "111111111111111111"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "unchanged"
    assert await _audit_entries(session_factory) == []


async def test_db_discord_id_conflict_rejected(session_factory, clean_db):
    await _seed_player(session_factory)
    await _seed_player(session_factory, discord_id=222222222222222222, ign="bob_")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "discord_id", "new_value": "222222222222222222"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "identitní konflikt" in result["message"]


async def test_db_discord_id_too_short_rejected(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "discord_id", "new_value": "123"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "podezřele" in result["message"]


# ---------------------------------------------------------------------------
# apply_player_edit – cooldowny (pole "cooldown")
# ---------------------------------------------------------------------------
async def test_db_cooldown_set_queue_applies_and_audits(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "cooldown", "action": "set_queue", "kit": "molepvp"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "changed"
    assert result["new_value"].startswith("waitlist molepvp:")
    assert "od teď" in result["new_value"]
    entries = await _audit_entries(session_factory)
    assert len(entries) == 1
    assert entries[0].details["field"] == "cooldown"


async def test_db_cooldown_set_queue_requires_kit(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "cooldown", "action": "set_queue"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"


async def test_db_cooldown_clear_queue_unchanged_when_empty(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "cooldown", "action": "clear_queue", "kit": "molepvp"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "unchanged"


async def test_db_cooldown_set_ht3_requires_kit(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "cooldown", "action": "set_ht3", "kit": ""},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "kit" in result["message"]


async def test_db_cooldown_set_ht3_applies_and_audits(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "cooldown", "action": "set_ht3", "kit": "molepvp"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "changed"
    assert result["new_value"].startswith("HT3 molepvp:")
    assert "od teď" in result["new_value"]
    entries = await _audit_entries(session_factory)
    assert len(entries) == 1
    assert entries[0].details["kit"] == "molepvp"


async def test_db_cooldown_unknown_action(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "cooldown", "action": "frobnicate"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "Neznámá akce" in result["message"]


# ---------------------------------------------------------------------------
# apply_player_edit – tier (pole "tier")
# ---------------------------------------------------------------------------
async def test_db_tier_change_applies_mirror_history_and_audit(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "HT3"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "changed"
    assert result["old_value"] == "LT3"
    assert result["new_value"] == "HT3"
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "molepvp")
        rows = (
            (
                await session.execute(
                    select(PlayerCurrentTier).where(
                        PlayerCurrentTier.player_id
                        == (
                            await PlayerRepository().get_by_discord_id(session, 111111111111111111)
                        ).id,
                        PlayerCurrentTier.kit_id == kit.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        current = rows[0]
        tier = await TierDefinitionRepository().get_by_id(session, current.tier_id)
        assert tier.code == "HT3"
        assert current.source == "manual"
        history = (
            (
                await session.execute(
                    select(TierHistory)
                    .where(TierHistory.player_id == current.player_id)
                    .order_by(TierHistory.changed_at)
                )
            )
            .scalars()
            .all()
        )
        assert len(history) >= 2  # seed (LT3) + aplikovaná změna (HT3)
    entries = await _audit_entries(session_factory)
    assert len(entries) == 1
    assert entries[0].details["field"] == "tier"
    assert entries[0].details["newValue"] == "HT3"


async def test_db_tier_unchanged(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "lt3"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "unchanged"
    assert await _audit_entries(session_factory) == []


async def test_db_tier_retired_archive_exact_value_allowed(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "RLT3", "retired": True},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "changed"
    assert result["new_value"] == "RLT3"
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "molepvp")
        player = await PlayerRepository().get_by_discord_id(session, 111111111111111111)
        rows = (
            (
                await session.execute(
                    select(PlayerCurrentTier).where(
                        PlayerCurrentTier.player_id == player.id,
                        PlayerCurrentTier.kit_id == kit.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        tier = await TierDefinitionRepository().get_by_id(session, rows[0].tier_id)
        assert tier.code == "RLT3"
        assert tier.kind == "virtual"


async def test_db_tier_retired_over_different_current_rejected(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "RHT3", "retired": True},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "NESMÍ" in result["message"]


async def test_db_tier_current_over_retired_rejected(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    # Archivuj LT3 → RLT3
    await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "RLT3", "retired": True},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    # Teď nelze přepsat retired aktuálním tierem (jde jen přes /result)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "HT3"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW + 60_000,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "retired" in result["message"]


async def test_db_tier_unknown_kit_rejected(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "neexistuje", "tier": "HT3"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "není v databázi" in result["message"]


# ---------------------------------------------------------------------------
# apply_player_edit – not_found / invalid field
# ---------------------------------------------------------------------------
async def test_db_unknown_player_not_found(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(999999999999999999),
        edit={"field": "ign", "new_value": "X"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "not_found"


async def test_db_non_numeric_player_id_not_found(session_factory, clean_db):
    result = await edituser.apply_player_edit(
        player_id="abc-not-a-discord-id",
        edit={"field": "ign", "new_value": "X"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "not_found"


async def test_db_unknown_field_rejected(session_factory, clean_db):
    await _seed_player(session_factory)
    result = await edituser.apply_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "hokus", "new_value": "X"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
    )
    assert result["status"] == "error"
    assert "Neznámé pole" in result["message"]


# ---------------------------------------------------------------------------
# execute_player_edit – DB: player_after a web canonical z PostgreSQL (F10)
# ---------------------------------------------------------------------------
async def test_db_execute_player_after_built_from_db(session_factory, clean_db):
    """Bez dodaného player_after se kanonická podoba staví z DB (build_player_shape)."""
    await _seed_player(session_factory, tier_code="LT3")
    calls = {}

    async def apply_roles(actions):
        calls["actions"] = list(actions)
        return [{"op": a.get("op"), "roleId": a.get("roleId"), "ok": True} for a in actions]

    report = await edituser.execute_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "HT3"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
        role_context={
            "member": {"id": 111111111111111111, "roles": []},
            "roles_map": {"molepvp": {"HT3": 777001}},
            "kit_display": {"molepvp": "MolePVP"},
        },
        apply_roles=apply_roles,
    )
    assert report["status"] == "SUCCESS"
    # player_after z DB → role plán vidí nový HT3 a snaží se roli přidat
    assert calls["actions"], "role plán z build_player_shape měl naplánovat akce"
    assert all(a["op"] == "add" for a in calls["actions"])


async def test_db_execute_web_canonical_from_export_players(session_factory, clean_db):
    """Web push dostane canonical list z export_players – nikdy JSON read."""
    await _seed_player(session_factory, tier_code="LT3")
    captured = {}

    async def push_web(canonical):
        captured["canonical"] = canonical
        return {"ok": True, "message": "ok"}

    report = await edituser.execute_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "ign", "new_value": "MenduTwo"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
        push_web=push_web,
    )
    assert report["status"] == "SUCCESS"
    canonical = captured["canonical"]
    assert isinstance(canonical, list)
    assert any(p.get("username") == "MenduTwo" for p in canonical)
    assert any(p.get("discordId") == "111111111111111111" for p in canonical)


async def test_db_execute_unchanged_skips_roles_and_web(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    applied = []

    async def apply_roles(actions):
        applied.extend(actions)
        return []

    report = await edituser.execute_player_edit(
        player_id=str(111111111111111111),
        edit={"field": "tier", "kit": "molepvp", "tier": "LT3"},
        actor_id=ACTOR_ID,
        actor_name=ACTOR_NAME,
        now=NOW,
        queue_cooldown_ms=QUEUE_MS,
        ht3_cooldown_ms=HT3_MS,
        session_factory=session_factory,
        role_context={"member": None, "roles_map": {}, "kit_display": {}},
        apply_roles=apply_roles,
        push_web=lambda canonical: None,
    )
    assert report["status"] == "SUCCESS"
    assert report["db"]["status"] == "unchanged"
    assert applied == []


# ---------------------------------------------------------------------------
# player_export – canonical shapes (Krok B)
# ---------------------------------------------------------------------------
async def test_db_build_player_shape_canonical(session_factory, clean_db):
    await _seed_player(session_factory, tier_code="LT3")
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 111111111111111111)
        shape = await player_export.build_player_shape(session, player)
    assert shape["username"] == "mendu__"
    assert shape["discordId"] == "111111111111111111"
    assert shape["modes"] == {"MolePVP": "LT3"}
    history = shape["history"]["MolePVP"]
    assert history and history[-1]["tier"] == "LT3"
    assert all("date" in h and "tier" in h for h in history)
    # history vzestupně (poslední = nejnovější)
    dates = [h["date"] for h in history]
    assert dates == sorted(dates)


async def test_db_build_player_shape_omits_discord_id_when_none(session_factory, clean_db):
    async with transaction(session_factory) as session:
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=444444444444444444, ign="nodisc"
        )
        # smaž discord_id → neclaimnutý hráč
        player.discord_id = None
        await session.flush()
        shape = await player_export.build_player_shape(session, player)
    assert "discordId" not in shape
    assert shape["modes"] == {}


async def test_db_export_players_returns_all(session_factory, clean_db):
    await _seed_player(session_factory)
    await _seed_player(session_factory, discord_id=222222222222222222, ign="bob_")
    shapes = await player_export.export_players(session_factory)
    assert len(shapes) == 2
    users = {s["username"] for s in shapes}
    assert users == {"mendu__", "bob_"}


# ---------------------------------------------------------------------------
# player_export – deterministický F5 exportér (todo #11)
# ---------------------------------------------------------------------------
async def test_db_write_players_export_deterministic(
    session_factory, clean_db, monkeypatch, tmp_path
):
    await _seed_player(session_factory)
    await _seed_player(session_factory, discord_id=222222222222222222, ign="bob_")
    monkeypatch.setattr("storage.DATA_DIR", str(tmp_path))
    exported = await player_export.write_players_export(session_factory)

    path = tmp_path / "players.json"
    assert path.exists()
    with open(path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == exported
    assert [p["username"] for p in on_disk] == ["mendu__", "bob_"]
    assert on_disk[0]["discordId"] == "111111111111111111"
    assert on_disk[0]["modes"] == {"MolePVP": "LT3"}

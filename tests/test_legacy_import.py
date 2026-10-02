"""tools.legacy_import — backup, preview (rollback), idempotent apply."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from db.models import (
    AuditLog,
    Cooldown,
    Evaluation,
    Kit,
    KitRole,
    Player,
    Result,
    Tester,
    TesterCredit,
    TierDefinition,
    TierHistory,
)
from db.repositories.evaluations import EvaluationRepository
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from tools.legacy_import import KV_TABLE, run_legacy_import

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
FUTURE_MS = int((NOW + timedelta(days=2)).timestamp() * 1000)
PAST_MS = int((NOW - timedelta(days=30)).timestamp() * 1000)


def _write(data_dir, name, data):
    (data_dir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    _write(d, "kits.json", ["NetheriteSword", "AnchorPvP"])
    _write(d, "kit_roles.json", {"NetheriteSword": {"LT2": "5001", "RLT2": "5002"}})
    _write(d, "players.json", [
        {"username": "Alice", "modes": {"NetheriteSword": "LT2"},
         "history": {"NetheriteSword": [
             {"date": "16.05.2026", "tier": "LT3"},
             {"date": "09.07.2026", "tier": "LT2"},
         ]}},
        {"username": "Bob", "modes": {"AnchorPvP": "RLT2"},
         "history": {"AnchorPvP": [{"date": "01.01.2026", "tier": "RLT2"}],
                     "OldKit": [{"date": "02.02.2025", "tier": "HT5"}]}},
    ])
    _write(d, "ht_results.json", {
        "900": {"id": "900", "kind": "ticket", "ticketId": "900", "playerId": "111",
                "ign": "Alice", "evaluatorId": "222", "kit": "NetheriteSword",
                "previousTier": "HT3", "newTier": "LT2", "outcome": "", "score": "3-1",
                "timestamp": int(datetime(2026, 7, 9, tzinfo=timezone.utc).timestamp() * 1000),
                "date": "09.07.2026", "resultType": "normal"},
        "htfight-111-1": {"id": "htfight-111-1", "kind": "ht_fight", "playerId": "333",
                          "ign": "Carl", "evaluatorId": "222", "kit": "NetheriteSword",
                          "previousTier": "HT3", "newTier": "HT3", "outcome": "Lost",
                          "score": "1-3", "opponentId": "111", "opponentName": "Alice",
                          "fightTier": "LT2", "timestamp": 1_780_000_000_000,
                          "resultType": "ht_fight", "announcement": "sent"},
    })
    _write(d, "evals.json", {"netheritesword": {"alice": 1_780_000_000_000}})
    _write(d, "testers.json", ["222"])
    _write(d, "cooldowns.json", {"111": FUTURE_MS, "333": PAST_MS})
    _write(d, "ht3_cooldowns.json", {"111": {"AnchorPvP": FUTURE_MS}})
    _write(d, "testers_stats.json", {"222": {"total": 10, "monthly": {"07.2026": 4, "06.2026": 3}}})
    _write(d, "websync_log.json", [{"ts": 1, "action": "push"}])
    return d


async def _counts(session_factory):
    async with transaction(session_factory) as session:
        out = {}
        for model in (Kit, KitRole, Player, TierHistory, Result, Evaluation, Tester,
                      Cooldown, TesterCredit, AuditLog):
            out[model.__tablename__] = (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        return out


async def _run(session_factory, data_dir, tmp_path, *, apply):
    return await run_legacy_import(
        session_factory, data_dir=data_dir, backup_root=tmp_path / "backups",
        apply=apply, now=NOW,
    )


async def test_preview_writes_nothing_but_backs_up(session_factory, clean_db, data_dir, tmp_path):
    before = await _counts(session_factory)
    summary = await _run(session_factory, data_dir, tmp_path, apply=False)
    assert await _counts(session_factory) == before
    assert summary["counts"]["results_created"] == 2
    backup = tmp_path / "backups" / summary["backup_dir"].split("/")[-1]
    manifest = json.loads((backup / "manifest.json").read_text())
    assert {s["name"] for s in manifest["sources"]} >= {"players.json", "ht_results.json"}
    assert "players" in manifest["tables"]
    assert (backup / "preview_report.json").exists()


async def test_apply_imports_everything(session_factory, clean_db, data_dir, tmp_path):
    summary = await _run(session_factory, data_dir, tmp_path, apply=True)
    c = summary["counts"]
    assert c["kits_created"] == 2
    assert c["kits_created_inactive"] == 1  # OldKit z historie
    assert c["kit_roles_created"] == 2
    assert c["history_created"] == 4
    assert c["results_created"] == 2
    assert c["evals_created"] == 1
    assert c["testers_created"] == 1
    assert c["cooldowns_created"] == 2
    assert c["cooldowns_expired_skipped"] == 1

    async with transaction(session_factory) as session:
        alice = await PlayerRepository().get_by_discord_id(session, 111)
        assert alice is not None and alice.ign == "Alice"  # adoptace legacy záznamu
        rlt2 = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "RLT2")
        )).scalar_one()
        lt2 = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "LT2")
        )).scalar_one()
        assert rlt2.kind == "retired" and rlt2.retired_of_id == lt2.id
        assert lt2.rank == 7
        fight = (await session.execute(
            select(Result).where(Result.result_key == "ht_fight:htfight-111-1")
        )).scalar_one()
        assert fight.opponent_id == 111 and fight.outcome == "Lost" and fight.subtype == "LT2"
        assert (await session.execute(
            select(Result).where(Result.result_key == "result:900")
        )).scalar_one().kind == "ticket"
        old = (await session.execute(select(Kit).where(Kit.key == "oldkit"))).scalar_one()
        assert old.active is False
        credits = {
            r.month: r.amount
            for r in (await session.execute(select(TesterCredit))).scalars()
        }
        # 07.2026: 4 v JSONu − 1 importovaný výsledek; 06.2026: 3; zbytek do „legacy“.
        assert credits == {"07.2026": 3, "06.2026": 3, "legacy": 3}
        archived = (await session.execute(
            select(AuditLog.entity_id).where(AuditLog.action == "legacy_archive")
        )).scalars().all()
        assert "websync_log.json" in archived


async def test_second_apply_is_noop(session_factory, clean_db, data_dir, tmp_path):
    await _run(session_factory, data_dir, tmp_path, apply=True)
    first = await _counts(session_factory)
    summary = await _run(session_factory, data_dir, tmp_path, apply=True)
    after = await _counts(session_factory)
    assert summary["noop"] is True, summary["counts"]
    first["audit_logs"] += 1  # jen souhrnný audit záznam druhého běhu
    assert after == first


async def test_existing_db_state_is_never_overwritten(session_factory, clean_db, data_dir, tmp_path):
    async with transaction(session_factory) as session:
        kit = Kit(key="netheritesword", name="NetheriteSword", active=True)
        session.add(kit)
        _c, alice = await PlayerRepository().claim_discord_id(session, discord_id=111, ign="AliceNew")
        await session.flush()
        ev = await EvaluationRepository().grant(session, player_id=alice.id, kit_id=kit.id)
        await EvaluationRepository().revoke(session, player_id=alice.id, kit_id=kit.id)
        session.add(Cooldown(player_id=alice.id, cooldown_type="waitlist", kit_id=None,
                             expires_at=NOW + timedelta(days=10), source="result"))
        assert ev is not None

    await _run(session_factory, data_dir, tmp_path, apply=True)

    async with transaction(session_factory) as session:
        alice = await PlayerRepository().get_by_discord_id(session, 111)
        assert alice.ign == "AliceNew"  # legacy IGN nepřejmenuje existujícího hráče
        evals = (await session.execute(
            select(Evaluation).where(Evaluation.player_id == alice.id)
        )).scalars().all()
        assert len(evals) == 1 and evals[0].revoked_at is not None  # odebraný eval se nevrací
        cd = (await session.execute(
            select(Cooldown).where(Cooldown.player_id == alice.id, Cooldown.kit_id.is_(None))
        )).scalar_one()
        assert cd.expires_at == NOW + timedelta(days=10)  # delší cooldown se nezkracuje


async def test_testers_not_imported_when_db_already_manages_them(
    session_factory, clean_db, data_dir, tmp_path
):
    async with transaction(session_factory) as session:
        _c, someone = await PlayerRepository().claim_discord_id(session, discord_id=999, ign="Mod")
        session.add(Tester(player_id=someone.id))
    summary = await _run(session_factory, data_dir, tmp_path, apply=True)
    assert summary["counts"].get("testers_created", 0) == 0
    assert summary["counts"]["testers_skipped_db_authoritative"] == 1


async def test_kv_table_wins_over_file(session_factory, clean_db, data_dir, tmp_path, db_engine):
    async with db_engine.begin() as conn:
        await conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {KV_TABLE} (key TEXT PRIMARY KEY, value JSONB NOT NULL,"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        ))
        await conn.execute(
            text(f"INSERT INTO {KV_TABLE} (key, value) VALUES ('kits.json', CAST(:v AS JSONB))"),
            {"v": json.dumps(["UHCMace"])},
        )
    try:
        summary = await _run(session_factory, data_dir, tmp_path, apply=True)
        assert summary["sources"]["kits.json"] == "kv"
        assert summary["conflicts_kv_over_file"] == ["kits.json"]
        async with transaction(session_factory) as session:
            uhc = (await session.execute(select(Kit).where(Kit.key == "uhcmace"))).scalar_one()
            assert uhc.active is True
    finally:
        async with db_engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {KV_TABLE}"))


async def test_kit_roles_for_unknown_kit_create_active_kit(session_factory, clean_db, data_dir, tmp_path):
    from db.services.config_validation import validate_kit_role_configuration

    _write(data_dir, "kit_roles.json", {"NetheriteSword": {"LT2": "5001"}, "NewKit": {"LT5": "5003"}})
    await _run(session_factory, data_dir, tmp_path, apply=True)
    async with transaction(session_factory) as session:
        new = (await session.execute(select(Kit).where(Kit.key == "newkit"))).scalar_one()
        assert new.active is True
        result = await validate_kit_role_configuration(session, guild_role_ids={5001, 5003})
    assert result.mapping_count == 2


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), ("", None), ("preview", "preview"), ("APPLY", "apply"), ("yes", None)],
)
def test_startup_mode(monkeypatch, value, expected):
    from tools.legacy_import import startup_mode

    if value is None:
        monkeypatch.delenv("LEGACY_IMPORT", raising=False)
    else:
        monkeypatch.setenv("LEGACY_IMPORT", value)
    assert startup_mode() == expected


@pytest.mark.parametrize("mode", ["preview", "apply"])
async def test_bot_startup_runs_legacy_import_when_requested(monkeypatch, mode):
    from unittest import mock

    import bot

    monkeypatch.setenv("LEGACY_IMPORT", mode)
    monkeypatch.setenv("AUTO_MIGRATE", "0")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    run = mock.AsyncMock(return_value={
        "mode": mode, "backup_dir": "/x", "sources": {}, "conflicts_kv_over_file": [],
        "unreadable": [], "counts": {}, "issues": [], "noop": True,
    })
    with (
        mock.patch("db.validation.validate_configured_role_ids", return_value=[]),
        mock.patch("db.validation.validate_database",
                   mock.AsyncMock(return_value={"schema_revision": "x"})),
        mock.patch("tools.legacy_import.run_legacy_import", run),
    ):
        engine, _sf = await bot._init_database()
        await engine.dispose()
    assert run.await_args.kwargs["apply"] is (mode == "apply")


async def test_runtime_config_filled_into_bot_config(session_factory, clean_db, data_dir, tmp_path):
    from services import config_store

    _write(data_dir, "queue_channels.json", {"NetheriteSword": "7001", "AnchorPvP": "7002"})
    _write(data_dir, "ht3_panel_message.json", {"message_id": "8001", "channel_id": "8002"})
    await config_store.set_queue_channel_id("anchorpvp", 9999, session_factory=session_factory)

    summary = await _run(session_factory, data_dir, tmp_path, apply=True)
    assert summary["counts"]["queue_channels_created"] == 1
    assert summary["counts"]["queue_channels_existing"] == 1
    assert await config_store.get_queue_channel_id("netheritesword", session_factory=session_factory) == 7001
    # /addqchannel nastavený v DB má přednost – nepřepisuje se.
    assert await config_store.get_queue_channel_id("anchorpvp", session_factory=session_factory) == 9999
    assert await config_store.get_ht3_panel(session_factory=session_factory) == {
        "message_id": "8001", "channel_id": "8002",
    }
    again = await _run(session_factory, data_dir, tmp_path, apply=True)
    assert again["noop"] is True


async def test_empty_kit_keys_are_reported_instead_of_crashing(
    session_factory, clean_db, data_dir, tmp_path
):
    _write(data_dir, "players.json", [
        {"username": "Alice", "modes": {"": "LT2"}, "history": {"": [{"date": "16.05.2026", "tier": "LT3"}]}},
    ])
    _write(data_dir, "evals.json", {"": {"alice": 1_780_000_000_000}})
    _write(data_dir, "ht3_cooldowns.json", {"111": {"": FUTURE_MS}})
    _write(data_dir, "kit_roles.json", {"": {"LT2": "5001"}})
    summary = await _run(session_factory, data_dir, tmp_path, apply=True)
    issues = summary.get("issues") or summary["counts"]
    assert issues

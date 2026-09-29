"""Phase E, E10 — PostgreSQL backup/restore verified in an isolated test DB.

Uses the embedded PostgreSQL server (Unix socket, no TCP) from conftest:
- seeds real relational data (players, tiers, history, cooldowns, testers,
  results, outbox, sync audit) into ``pytest_phase_e10_src``;
- runs a real ``pg_dump`` (custom format) → ``pg_restore`` into a fresh
  ``pytest_phase_e10_dst`` database;
- verifies row counts and key values are bit-identical after restore;
- verifies the Phase D JSON backup (``backups/phase_d/*/``) is intact:
  ``restore_backup(verify_only=True)`` passes and manifests are unchanged.

No production database is touched: every database name is a fresh isolated
test database on the embedded server.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from db.models import (
    AuditLog,
    BotConfig,
    Cooldown,
    Kit,
    MigrationImportIssue,
    OutboxEvent,
    Player,
    PlayerCurrentTier,
    Result,
    SyncAction,
    SyncRun,
    TierDefinition,
    TierHistory,
)

# L3 audit fix: resolve pg_dump/pg_restore via the embedded_postgres
# package's own install path instead of a hardcoded venv layout — the
# previous hardcoded ".venv/lib/python3.13/..." path broke as soon as the
# venv was rebuilt against a different Python version (reproduced directly:
# the 3.13 interpreter no longer existed on this machine).
from embedded_postgres._commands import POSTGRES_BIN_PATH as PG_BIN

from services.phase_d.backup import backup_data_dir, load_manifest, restore_backup

REPO_ROOT = Path(__file__).resolve().parents[1]


def _table_models() -> dict[str, object]:
    from db.models import Tester

    return {
        "players": Player,
        "kits": Kit,
        "tier_definitions": TierDefinition,
        "player_current_tiers": PlayerCurrentTier,
        "tier_history": TierHistory,
        "results": Result,
        "cooldowns": Cooldown,
        "testers": Tester,
        "outbox_events": OutboxEvent,
        "sync_runs": SyncRun,
        "sync_actions": SyncAction,
        "audit_logs": AuditLog,
        "migration_import_issues": MigrationImportIssue,
        "bot_config": BotConfig,
    }

ALL_SEED_TABLES = (
    "players",
    "kits",
    "tier_definitions",
    "player_current_tiers",
    "tier_history",
    "results",
    "tickets",
    "ticket_members",
    "cooldowns",
    "queues",
    "queue_entries",
    "evaluations",
    "testers",
    "outbox_events",
    "sync_runs",
    "sync_actions",
    "audit_logs",
    "bot_config",
    "migration_import_issues",
)

# URL shapes: postgresql://postgres:@/{db}?host={socket_dir} (sync)
#            postgresql+asyncpg://postgres:@/{db}?host={socket_dir} (async)
def _sync_url(socket_dir: str, database: str) -> str:
    return f"postgresql://postgres:@/{database}?host={socket_dir}"


def _async_url(socket_dir: str, database: str) -> str:
    return "postgresql+asyncpg://" + _sync_url(socket_dir, database).split("://", 1)[1]


def _drop_create(socket_dir: str, database: str) -> None:
    import psycopg

    with psycopg.connect(
        _sync_url(socket_dir, "postgres"), autocommit=True
    ) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{database}"')
        conn.execute(f'CREATE DATABASE "{database}"')


def _alembic_upgrade(socket_dir: str, database: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _sync_url(socket_dir, database))
    command.upgrade(cfg, "head")


def _make_session_factory(socket_dir: str, database: str):
    from sqlalchemy.pool import NullPool

    from db.engine import create_async_engine_from_url, make_session_factory

    engine = create_async_engine_from_url(
        _async_url(socket_dir, database), poolclass=NullPool
    )
    return engine, make_session_factory(engine)


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _seed_rich_rows(session_factory) -> None:
    from pathlib import Path as _Path

    from db.repositories.outbox import OutboxRepository
    from db.repositories.sync_audit import (
        AuditRepository,
        SyncActionRepository,
        SyncRunRepository,
    )
    from services.phase_d.import_data import import_json_data

    data_dir = _Path("/tmp/opencode/e10_seed_json")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "kits.json").write_text(
        json.dumps(["IronAxe", "RandomPot"]), encoding="utf-8"
    )
    (data_dir / "players.json").write_text(
        json.dumps(
            [
                {
                    "username": "AliceMC",
                    "modes": {"IronAxe": "LT3"},
                    "history": {
                        "IronAxe": [
                            {"date": "01.01.2026", "tier": "LT3"},
                            {"date": "05.01.2026", "tier": "HT3"},
                        ]
                    },
                },
                {
                    "username": "bob_",
                    "modes": {},
                    "history": {"NoSuchKit": [{"date": "10.10.2026", "tier": "LT2"}]},
                },
            ]
        ),
        encoding="utf-8",
    )
    (data_dir / "cooldowns.json").write_text(
        json.dumps({"1111111111111111111": 1780593929525}), encoding="utf-8"
    )
    (data_dir / "ht3_cooldowns.json").write_text(
        json.dumps({"1111111111111111111": {"IronAxe": 1785769543952}}),
        encoding="utf-8",
    )
    (data_dir / "testers.json").write_text(
        json.dumps(["1111111111111111111", "999999999999999999"]), encoding="utf-8"
    )

    report = await import_json_data(session_factory, data_dir=data_dir)
    assert report["players_created"] == 2
    assert report["kits_ensured"] == 2

    async with session_factory() as session:
        run = await SyncRunRepository().start(
            session, command="/sync discord", mode="apply"
        )
        await SyncRunRepository().finish(
            session, sync_run_id=run.id, status="success"
        )
        await OutboxRepository().enqueue(
            session,
            event_type="tier_change",
            aggregate_type="player",
            aggregate_id="1",
            payload={"tier": "LT3"},
        )
        sync_run = (await session.execute(select(SyncRun))).scalars().first()
        await SyncActionRepository().record(
            session,
            sync_run_id=sync_run.id,
            action_type="role_added",
            member_id=999,
            status="applied",
        )
        await AuditRepository().append(
            session, action="phase_e_test", entity_type="test", entity_id="e10"
        )
        await session.commit()

    from datetime import datetime, timezone

    from db.models import Player
    from db.repositories.tiers import MirrorServiceRepository

    async with session_factory() as session:
        alice = (
            await session.execute(
                select(Player).where(Player.ign == "AliceMC")
            )
        ).scalars().first()
        kit = (await session.execute(select(Kit).where(Kit.key == "IronAxe"))).scalars().first()
        tier = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "LT3")
            )
        ).scalars().first()
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=alice.id,
            kit_id=kit.id,
            tier_id=tier.id,
            observed_at=datetime.now(timezone.utc),
            source="discord_sync",
        )
        await session.commit()


@pytest.fixture
async def e10_db(embedded_pg):
    socket_dir = str(embedded_pg)
    _drop_create(socket_dir, "pytest_phase_e10_src")
    _drop_create(socket_dir, "pytest_phase_e10_dst")
    _alembic_upgrade(socket_dir, "pytest_phase_e10_src")
    engine, factory = _make_session_factory(socket_dir, "pytest_phase_e10_src")
    try:
        await _seed_rich_rows(factory)
        yield socket_dir, factory, _alembic_upgrade
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pg_dump_restore_roundtrip_preserves_rows(e10_db):
    socket_dir, factory, _ = e10_db
    seeded = {}
    for table, model in _table_models().items():
        seeded[table] = await _count(factory, model)
    for must_have_data in ("players", "kits", "tier_definitions", "tier_history"):
        assert seeded[must_have_data] >= 1, f"{must_have_data} musí mít seed data"

    dump_path = Path("/tmp/opencode/e10_pg.dump")
    pg_dump = PG_BIN / "pg_dump"
    pg_restore = PG_BIN / "pg_restore"
    dump_cmd = [
        str(pg_dump), "-Fc", "-f", str(dump_path),
        "-h", socket_dir, "-U", "postgres", "-d", "pytest_phase_e10_src",
    ]
    subprocess.run(dump_cmd, check=True, capture_output=True)

    restore_cmd = [
        str(pg_restore), "-Fc",
        "-h", socket_dir, "-U", "postgres", "-d", "pytest_phase_e10_dst",
        str(dump_path),
    ]
    dst_engine, dst_factory = _make_session_factory(socket_dir, "pytest_phase_e10_dst")
    try:
        subprocess.run(restore_cmd, check=True, capture_output=True)
        for table, expected_count in seeded.items():
            assert await _count(dst_factory, _table_models()[table]) == expected_count, (
                f"{table}: {expected_count} → restore neodpovídá"
            )
    finally:
        await dst_engine.dispose()
        dump_path.unlink(missing_ok=True)


@pytest.fixture
def phase_d_backup(tmp_path) -> Path:
    """Záloha ze syntetické data/ složky (skutečná data hráčů v gitu nejsou)."""
    data = tmp_path / "data"
    data.mkdir()
    fixtures = {
        "players.json": [{"username": "Alice", "modes": {}, "history": {}}],
        "kits.json": ["NetheriteSword"],
        "testers.json": ["111"],
        "cooldowns.json": {"111": 1_780_000_000_000},
        "ht3_cooldowns.json": {"111": {"NetheriteSword": 1_780_000_000_000}},
        "testers_stats.json": {"111": {"total": 1, "monthly": {"09.2026": 1}}},
    }
    for name, payload in fixtures.items():
        (data / name).write_text(json.dumps(payload), encoding="utf-8")
    backup_dir, _manifest = backup_data_dir(data, dest_root=tmp_path / "backups")
    return backup_dir


@pytest.mark.asyncio
async def test_phase_d_json_backup_intact_and_verify_only(e10_db, phase_d_backup):
    backup_dir = phase_d_backup
    manifest = load_manifest(backup_dir)
    assert manifest["summary"]["files"] >= 5

    report = restore_backup(backup_dir, verify_only=True)
    assert report["verify_only"] is True
    assert "restored" not in report

    for entry in manifest["files"]:
        raw = (backup_dir / entry["name"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"], entry["name"]


@pytest.mark.asyncio
async def test_restore_never_reaches_discord(e10_db, tmp_path, phase_d_backup):
    _socket_dir, _factory, _upgrade = e10_db
    backup_dir = phase_d_backup

    data = tmp_path / "restore_target"
    report = restore_backup(backup_dir, data)
    assert len(report["restored"]) >= 5
    for name in report["restored"]:
        assert (data / name).exists()


@pytest.mark.asyncio
async def test_backup_markers_never_contain_connection_string(e10_db):
    socket_dir, factory, _ = e10_db
    async with factory() as session:
        rows = list((await session.execute(select(AuditLog))).scalars())
    assert rows
    for row in rows:
        dump = json.dumps(row.details).lower()
        for secret in ("password", "token", "database_url", "dsn", "host=", "postgresql"):
            assert secret not in dump
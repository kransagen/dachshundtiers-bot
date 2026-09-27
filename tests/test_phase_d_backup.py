"""Phase D, D1 — backup/restore/marker tests (hashed byte-for-byte tooling)."""

from __future__ import annotations

import json

import pytest

from db.models import AuditLog
from db.repositories.sync_audit import AuditRepository, BotConfigRepository
from db.services.session import transaction

from services.phase_d.backup import (
    backup_data_dir,
    list_backups,
    record_backup_markers,
    restore_backup,
)

PLAYERS_RAW = [
    {"username": "AliceMC", "modes": {"IronAxe": "LT3"}, "history": {}},
    {"username": "bob_", "modes": {}, "history": {"IronAxe": [{"date": "01.01.2025", "tier": "HT3"}]}},
]


def _write_players(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "players.json").write_text(
        json.dumps(PLAYERS_RAW, ensure_ascii=False), encoding="utf-8"
    )
    (path / "cooldowns.json").write_text(
        json.dumps({"1111111111111111111": 1780593929525}), encoding="utf-8"
    )


def test_backup_copies_bytes_and_manifest_hashes(tmp_path):
    data = tmp_path / "data"
    _write_players(data)
    dest, manifest = backup_data_dir(data, dest_root=tmp_path / "backups")

    assert manifest["summary"]["files"] == 2
    assert manifest["summary"]["records"] == 3  # 2 players + 1 cooldown
    assert (dest / "players.json").read_bytes() == (data / "players.json").read_bytes()
    assert (dest / "manifest.json").exists()
    assert (dest / "manifest.md").exists()

    for entry in manifest["files"]:
        raw = (dest / entry["name"]).read_bytes()
        import hashlib

        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
        assert entry["readable"] is True
    players_entry = next(e for e in manifest["files"] if e["name"] == "players.json")
    assert players_entry["json_kind"] == "list"
    assert players_entry["records"] == 2


def test_backup_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_data_dir(tmp_path / "nope", dest_root=tmp_path / "backups")


def test_backup_unreadable_file_flagged_not_dropped(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "broken.json").write_bytes(b"{not json\xff")
    dest, manifest = backup_data_dir(data, dest_root=tmp_path / "backups")

    entry = manifest["files"][0]
    assert entry["readable"] is False
    assert entry["records"] is None
    assert "reason" in entry
    assert (dest / "broken.json").exists()
    assert (dest / "broken.json").read_bytes() == b"{not json\xff"


def test_restore_roundtrip_byte_identical(tmp_path):
    data = tmp_path / "data"
    _write_players(data)
    dest, manifest = backup_data_dir(data, dest_root=tmp_path / "backups")
    original = (data / "players.json").read_bytes()

    (data / "players.json").write_text(json.dumps([{"username": "hacker"}]), encoding="utf-8")
    report = restore_backup(dest, data)

    assert report["restored"] == ["cooldowns.json", "players.json"]
    assert (data / "players.json").read_bytes() == original
    assert manifest["files"][1]["sha256"]


def test_restore_refuses_tampered_backup(tmp_path):
    data = tmp_path / "data"
    _write_players(data)
    dest, _ = backup_data_dir(data, dest_root=tmp_path / "backups")
    original = (data / "players.json").read_bytes()

    (dest / "players.json").write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="sha256 nesouhlasí"):
        restore_backup(dest, data)
    assert (data / "players.json").read_bytes() == original


def test_restore_verify_only_writes_nothing(tmp_path):
    data = tmp_path / "data"
    _write_players(data)
    dest, _ = backup_data_dir(data, dest_root=tmp_path / "backups")

    (data / "players.json").unlink()
    report = restore_backup(dest, data, verify_only=True)

    assert report["verify_only"] is True
    assert not (data / "players.json").exists()
    assert "restored" not in report


def test_list_backups_sorted_newest_first(tmp_path):
    dest_root = tmp_path / "backups"
    data = tmp_path / "data"
    _write_players(data)

    first, _ = backup_data_dir(data, dest_root=dest_root)
    second, _ = backup_data_dir(data, dest_root=dest_root)

    backups = list_backups(dest_root)
    assert [b["dir"] for b in backups] == [str(second), str(first)]
    assert backups[0]["summary"]["files"] == 2


@pytest.mark.asyncio
async def test_record_backup_markers_writes_audit_and_config(session_factory, clean_db):
    manifest = {
        "backend": "postgresql",
        "git_rev": "abc123",
        "alembic_head": "a1b2c3d4e5f6",
        "summary": {"files": 2, "bytes": 42, "records": 3, "unreadable": 0},
    }
    await record_backup_markers(session_factory, entity_id="20260925T000000Z", manifest=manifest)

    async with transaction(session_factory) as session:
        audit = await AuditRepository().list(session, entity_type="backup")
        assert len(audit) == 1
        assert audit[0].action == "phase_d_backup"
        details = audit[0].details
        assert details["files"] == 2
        assert "password" not in json.dumps(details).lower()

        latest = await BotConfigRepository().get(session, "phase_d.backup.latest")
        assert latest["entity_id"] == "20260925T000000Z"
        assert latest["details"]["backend"] == "postgresql"


@pytest.mark.asyncio
async def test_record_backup_markers_never_contains_secrets(session_factory, clean_db):
    manifest = {
        "backend": "postgresql",
        "summary": {"files": 1, "bytes": 1, "records": 1, "unreadable": 0},
    }
    await record_backup_markers(session_factory, entity_id="sec", manifest=manifest)
    async with transaction(session_factory) as session:
        rows = list((await session.execute(__import__("sqlalchemy").select(AuditLog))).scalars())
        assert len(rows) == 1
        dump = json.dumps(rows[0].details).lower()
        for secret in ("password", "token", "database_url", "dsn"):
            assert secret not in dump
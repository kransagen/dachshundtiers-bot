"""Startup role-ID and migration-head validation tests."""

import json
import os

import pytest

import storage
from db.config import DatabaseConfigError
from db.validation import migration_head, validate_configured_role_ids


def _write_kit_roles(tmp_path, data):
    os.makedirs(tmp_path, exist_ok=True)
    path = os.path.join(tmp_path, "kit_roles.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def test_missing_kit_roles_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    assert validate_configured_role_ids() == []


def test_valid_role_ids_returned(monkeypatch, tmp_path):
    _write_kit_roles(
        tmp_path,
        {
            "anchorpvp": {
                "HT3": 1524008928596201574,
                "LT1": "1505441297685549137",
            },
            "ironaxe": {"LT3": 1505441297685549137},
        },
    )
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    assert validate_configured_role_ids() == sorted(
        {1524008928596201574, 1505441297685549137, 1505441297685549137}
    )


def test_non_numeric_role_id_fails_fast(monkeypatch, tmp_path):
    _write_kit_roles(tmp_path, {"anchorpvp": {"HT3": "not-a-number"}})
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    with pytest.raises(DatabaseConfigError, match="Nečíselné role ID"):
        validate_configured_role_ids()


def test_invalid_structure_fails_fast(monkeypatch, tmp_path):
    _write_kit_roles(tmp_path, {"anchorpvp": [1, 2, 3]})
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    with pytest.raises(DatabaseConfigError):
        validate_configured_role_ids()


def test_corrupt_file_fails_fast(monkeypatch, tmp_path):
    path = os.path.join(str(tmp_path), "kit_roles.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    with pytest.raises(DatabaseConfigError, match="Poškozený"):
        validate_configured_role_ids()


def test_migration_head_is_available():
    head = migration_head()
    assert isinstance(head, str) and len(head) >= 12
    assert head == head.strip()


async def test_validate_database_accepts_migrated_schema(db_engine):
    from db.validation import validate_database

    result = await validate_database(db_engine)
    assert result == {
        "backend": "postgresql",
        "ok": True,
        "schema_revision": migration_head(),
    }


async def test_validate_database_rejects_empty_database(embedded_pg):
    from sqlalchemy.pool import NullPool

    from db.engine import create_async_engine_from_url, dispose_engine
    from db.validation import validate_database
    from tests.conftest import _async_url, _create_fresh_database

    _create_fresh_database(embedded_pg, "pytest_unmigrated")
    engine = create_async_engine_from_url(
        _async_url(embedded_pg, "pytest_unmigrated"), poolclass=NullPool
    )
    try:
        with pytest.raises(DatabaseConfigError, match="migrov"):
            await validate_database(engine)
    finally:
        await dispose_engine(engine)
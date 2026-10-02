"""DB config unit tests (no PostgreSQL needed)."""

import pytest

from db.config import (
    DatabaseConfigError,
    build_async_database_url,
    build_sync_database_url,
    database_url,
    strict_kit_roles_enabled,
)
from db.engine import create_async_engine_from_url


def test_build_async_url_normalizes_psycopg_scheme():
    assert (
        build_async_database_url("postgresql://u:p@host:5432/db")
        == "postgresql+asyncpg://u:p@host:5432/db"
    )
    assert (
        build_async_database_url("postgres://u:p@host:5432/db")
        == "postgresql+asyncpg://u:p@host:5432/db"
    )


def test_build_async_url_idempotent_and_empty():
    url = "postgresql+asyncpg://u:p@host/db"
    assert build_async_database_url(url) == url
    assert build_async_database_url("") == ""
    assert build_async_database_url(None) == ""


def test_build_sync_url_strips_asyncpg():
    assert (
        build_sync_database_url("postgresql+asyncpg://u:p@host/db")
        == "postgresql://u:p@host/db"
    )
    assert build_sync_database_url("postgresql://u:p@host/db") == "postgresql://u:p@host/db"


def test_build_rejects_unknown_scheme():
    with pytest.raises(DatabaseConfigError):
        build_async_database_url("mysql://u:p@host/db")


def test_database_required_symbol_removed():
    """PostgreSQL is unconditionally mandatory now — there is no more
    opt-in DB_REQUIRED flag (bot.py._init_database always raises on a
    missing DATABASE_URL, JSON-only deployment mode no longer exists)."""
    import db.config as dbconfig

    assert not hasattr(dbconfig, "database_required")


def test_strict_kit_roles_flag(monkeypatch):
    for value, expected in [
        ("", False),
        ("0", False),
        ("off", False),
        ("1", True),
        ("true", True),
        ("ON", True),
    ]:
        monkeypatch.setenv("STRICT_KIT_ROLES", value)
        assert strict_kit_roles_enabled() is expected


def test_database_url_reads_live_env(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:secret@db.example:5432/tiers"
    )
    assert database_url() == "postgresql://user:secret@db.example:5432/tiers"
    monkeypatch.setenv("DATABASE_URL", "")
    assert database_url() == ""


def test_missing_url_raises_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for var in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    assert database_url() == ""
    with pytest.raises(DatabaseConfigError, match="DATABASE_URL"):
        create_async_engine_from_url("")


def test_engine_error_message_contains_no_credentials(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for var in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(DatabaseConfigError) as exc:
        create_async_engine_from_url("")
    assert "postgresql" not in str(exc.value).lower()
    assert "heslo" in str(exc.value).lower() or "DATABASE_URL" in str(exc.value)

def test_build_async_url_translates_sslmode_for_asyncpg():
    assert (
        build_async_database_url("postgresql://u:p@host/db?sslmode=require")
        == "postgresql+asyncpg://u:p@host/db?ssl=require"
    )
    assert (
        build_async_database_url(
            "postgres://u:p@host/db?sslmode=verify-full&channel_binding=require&application_name=x"
        )
        == "postgresql+asyncpg://u:p@host/db?ssl=verify-full&application_name=x"
    )
    assert (
        build_async_database_url("postgresql+asyncpg://u:p@host/db?sslmode=require")
        == "postgresql+asyncpg://u:p@host/db?ssl=require"
    )


def test_sync_url_keeps_sslmode():
    url = "postgresql://u:p@host/db?sslmode=require"
    assert build_sync_database_url(url) == url


def test_db_parts_url_includes_sslmode(monkeypatch):
    for name, value in (
        ("DATABASE_URL", ""),
        ("DB_HOST", "h"),
        ("DB_NAME", "n"),
        ("DB_USER", "u"),
        ("DB_PASSWORD", "p"),
        ("DB_SSLMODE", "require"),
    ):
        monkeypatch.setenv(name, value)
    assert database_url() == "postgresql://u:p@h:5432/n?sslmode=require"
    monkeypatch.delenv("DB_SSLMODE")
    assert database_url() == "postgresql://u:p@h:5432/n"

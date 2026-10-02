"""Async engine/session lifecycle tests."""

import pytest

from db.config import DatabaseConfigError
from db.engine import (
    create_async_engine_from_url,
    dispose_engine,
    make_session_factory,
)
from db.models import Player


def test_engine_factory_rejects_empty_url():
    with pytest.raises(DatabaseConfigError):
        create_async_engine_from_url("")
    with pytest.raises(DatabaseConfigError):
        create_async_engine_from_url(None)


async def test_session_commit_and_query(db_engine, clean_db):
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add(Player(discord_id=1, ign="CommitMe"))
        await session.commit()
    async with factory() as session:
        rows = (
            await session.execute(Player.__table__.select())
        ).all()
    assert len(rows) == 1


async def test_dispose_is_safe_noop():
    await dispose_engine(None)


async def test_engine_dispose_releases_pool(db_engine):
    async with db_engine.connect() as conn:
        await conn.exec_driver_sql("SELECT 1")
    await db_engine.dispose()
    async with db_engine.connect() as conn:
        assert (await conn.exec_driver_sql("SELECT 1")).first()[0] == 1

def test_engine_timeouts_can_be_disabled_and_overridden(monkeypatch):
    from db import engine as engine_module

    captured = {}

    def fake_create(url, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(engine_module, "create_async_engine", fake_create)
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "0")
    monkeypatch.setenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "9000")
    create_async_engine_from_url("postgresql://u:p@localhost/db")
    assert captured["connect_args"]["server_settings"] == {
        "idle_in_transaction_session_timeout": "9000"
    }

    monkeypatch.setenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "0")
    create_async_engine_from_url("postgresql://u:p@localhost/db")
    assert captured["connect_args"] == {}


def test_engine_default_timeouts_passed_as_server_settings(monkeypatch):
    from db import engine as engine_module

    captured = {}
    monkeypatch.setattr(
        engine_module, "create_async_engine", lambda url, **kw: captured.update(kw)
    )
    monkeypatch.delenv("DB_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", raising=False)
    create_async_engine_from_url("postgresql://u:p@localhost/db")
    assert captured["connect_args"]["server_settings"] == {
        "statement_timeout": "120000",
        "idle_in_transaction_session_timeout": "300000",
    }

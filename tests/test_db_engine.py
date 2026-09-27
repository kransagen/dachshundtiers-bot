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
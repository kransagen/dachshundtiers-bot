"""Phase A test infrastructure.

``embedded-postgres`` (NOT the IoT "pyembedded" lib) supplies a session-scoped
PostgreSQL server. On Linux it binds a Unix socket only — there is no TCP port.
The async engine is function-scoped with ``NullPool`` so it lives on the same
event loop as the test (pytest-asyncio creates a fresh loop per test).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[1]

EMBEDDED_PG_DIR = Path(
    os.environ.get("EMBEDDED_PG_DIR", "/tmp/opencode/pip-test/pgembedded")
)

ALL_TABLES = (
    "players",
    "kits",
    "tier_definitions",
    "kit_roles",
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
    "tournaments",
    "tournament_entries",
    "tester_credits",
)


def _sync_url(socket_dir: str, database: str) -> str:
    return f"postgresql://postgres:@/{database}?host={socket_dir}"


def _async_url(socket_dir: str, database: str) -> str:
    return "postgresql+asyncpg://" + _sync_url(socket_dir, database).split("://", 1)[1]


def _alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _create_fresh_database(socket_dir: str, name: str) -> None:
    import psycopg

    with psycopg.connect(
        _sync_url(socket_dir, "postgres"), autocommit=True
    ) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')


@pytest.fixture(scope="session")
def embedded_pg() -> Iterator[str]:
    from embedded_postgres import get_server

    server = get_server(EMBEDDED_PG_DIR, cleanup_mode="stop")
    socket_dir = str(server.get_postmaster_info().socket_dir)
    yield socket_dir
    server.cleanup()


@pytest.fixture(scope="session")
def migrated_db_url(embedded_pg) -> Iterator[str]:
    from alembic import command

    _create_fresh_database(embedded_pg, "pytest_phase_a")
    url = _sync_url(embedded_pg, "pytest_phase_a")
    command.upgrade(_alembic_config(url), "head")
    return url


@pytest.fixture
async def db_engine(migrated_db_url) -> Iterator["AsyncEngine"]:
    from sqlalchemy.pool import NullPool

    from db.engine import create_async_engine_from_url, dispose_engine

    engine = create_async_engine_from_url(
        _async_url(_socket_dir_from_url(migrated_db_url), "pytest_phase_a"),
        poolclass=NullPool,
    )
    yield engine
    await dispose_engine(engine)


def _socket_dir_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(url).query)["host"][0]


@pytest.fixture
def session_factory(db_engine):
    from db.engine import make_session_factory

    return make_session_factory(db_engine)


@pytest.fixture
async def clean_db(db_engine):
    async with db_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE " + ", ".join(ALL_TABLES) + " RESTART IDENTITY CASCADE")
        )
    yield
"""Phase A test infrastructure.

``embedded-postgres`` (NOT the IoT "pyembedded" lib) supplies a session-scoped
PostgreSQL server. On Linux it binds a Unix socket only — there is no TCP port.
The async engine is function-scoped with ``NullPool`` so it lives on the same
event loop as the test (pytest-asyncio creates a fresh loop per test).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[1]

# EMBEDDED_PG_DIR override lets a machine pin a persistent pgdata location
# (faster repeat local runs); the default is a portable per-machine temp path
# so a fresh CI runner (no pre-existing /tmp layout) always works.
EMBEDDED_PG_DIR = Path(
    os.environ.get(
        "EMBEDDED_PG_DIR",
        str(Path(tempfile.gettempdir()) / "dachshundtiers-embedded-pg"),
    )
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
    "queue_testers",
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
    "minecraft_accounts",
    "player_link_tokens",
    "kit_tester_rooms",
    "player_peak_tiers",
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

    # get_server() requires its parent directory to already exist (it only
    # creates the leaf pgdata dir itself) — a fresh machine/CI runner won't
    # have EMBEDDED_PG_DIR.parent yet.
    EMBEDDED_PG_DIR.parent.mkdir(parents=True, exist_ok=True)
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
    """Reset every table before a test that needs an empty database.

    ``TRUNCATE ... CASCADE`` needs an AccessExclusiveLock on every table at
    once, so a single connection left behind by an earlier test (an
    unclosed ``AsyncSession``, or a task that outlived its test) parks this
    statement: such a backend is ``idle in transaction``, still holds an
    AccessShareLock, and the truncate waits indefinitely. When two such
    sessions overlap, PostgreSQL reports ``deadlock detected`` instead and
    kills the TRUNCATE. Both symptoms surface as scattered, order-dependent
    failures in tests that have nothing to do with the leaking one
    (IntegrityError on a unique index the truncate never got to clear, empty
    result sets, ...).

    Nothing outside this fixture may legitimately hold a connection: the
    engine is function-scoped with ``NullPool`` and is disposed on teardown,
    and pytest runs tests sequentially on this dedicated embedded server. So
    any OTHER backend on this database is a leak - terminate it first, then
    truncate. That makes per-test isolation deterministic instead of
    depending on GC timing.
    """
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid()"
            )
        )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE " + ", ".join(ALL_TABLES) + " RESTART IDENTITY CASCADE")
        )
    yield
"""Alembic environment (dual-mode: sync psycopg + async asyncpg).

``sqlalchemy.url`` from alembic.ini is intentionally empty; the URL comes
from the environment (``DATABASE_URL`` / ``DB_*`` via ``db.config``) or from
``alembic -x url='...'`` (used by tests against the embedded PostgreSQL).
A plain ``postgresql://`` URL runs synchronously (psycopg), an
``+asyncpg://`` URL runs on an async engine.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    # Same .env as the bot (config.py); real environment variables still win.
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from db import models  # noqa: E402,F401  (register all tables)
from db.base import Base  # noqa: E402
from db.config import build_sync_database_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url(explicit_x_url: str = "") -> str:
    url = config.get_main_option("sqlalchemy.url") or ""
    if url:
        return url
    if explicit_x_url:
        return explicit_x_url
    return build_sync_database_url()


def _detect_async(url: str) -> bool:
    return url.startswith("postgresql+asyncpg://") or url.startswith(
        "postgres+asyncpg://"
    )


def run_migrations_offline() -> None:
    url = _resolve_url(
        context.get_x_argument(as_dictionary=True).get("url", "")
    )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url(
        context.get_x_argument(as_dictionary=True).get("url", "")
    )
    if _detect_async(url):
        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine

        async def _run() -> None:
            engine = create_async_engine(url)
            try:
                async with engine.connect() as connection:
                    await connection.run_sync(_do_run_migrations)
            finally:
                await engine.dispose()

        asyncio.run(_run())
    else:
        from sqlalchemy import create_engine

        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                _do_run_migrations(connection)
        finally:
            engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
"""Async engine + session lifecycle for PostgreSQL (Phase A).

Threads the ``asyncio`` event loop through a single :class:`AsyncEngine`
per URL. Safe pool defaults (bound pool, ``pool_pre_ping``, bounded
``pool_recycle``) prevent stale connections to remote/managed PostgreSQL.
No global session is leaked: sessions are created per operation via an
:class:`async_sessionmaker` obtained from :func:`make_session_factory`.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Union

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import QueuePool

from db.config import DatabaseConfigError, build_async_database_url

log = logging.getLogger("dachshundtiers.db")

DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 10
DEFAULT_POOL_TIMEOUT = 30.0
DEFAULT_POOL_RECYCLE = 1800  # seconds; remote PG closes idle conns sooner
DEFAULT_POOL_PRE_PING = True
DEFAULT_STATEMENT_TIMEOUT_MS = 120_000
DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS = 300_000


def _timeout_ms(env_name: str, default: int) -> int:
    """Timeout z prostředí v ms; ``0`` ho vypne (např. za pgbouncerem)."""
    try:
        return max(0, int(os.getenv(env_name, "").strip() or default))
    except ValueError:
        return default


def create_async_engine_from_url(
    url: Optional[str],
    *,
    echo: bool = False,
    pool_size: int = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    pool_timeout: Union[float, int] = DEFAULT_POOL_TIMEOUT,
    pool_recycle: int = DEFAULT_POOL_RECYCLE,
    pool_pre_ping: bool = DEFAULT_POOL_PRE_PING,
    **kwargs,
) -> AsyncEngine:
    """Create an :class:`AsyncEngine` with safe pool defaults.

    ``kwargs`` pass through to :func:`create_async_engine` (e.g.
    ``poolclass=NullPool`` for short-lived test engines).
    """
    async_url = build_async_database_url(url)
    if not async_url:
        raise DatabaseConfigError(
            "Nelze vytvořit DB engine: chybí DATABASE_URL (nebo DB_HOST/DB_NAME/DB_USER/DB_PASSWORD)."
        )
    pool_kwargs = {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": pool_timeout,
        "pool_recycle": pool_recycle,
        "pool_pre_ping": pool_pre_ping,
    }
    connect_args = dict(kwargs.pop("connect_args", None) or {})
    server_settings = dict(connect_args.get("server_settings") or {})
    for setting, env_name, default in (
        ("statement_timeout", "DB_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS),
        (
            "idle_in_transaction_session_timeout",
            "DB_IDLE_IN_TRANSACTION_TIMEOUT_MS",
            DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS,
        ),
    ):
        timeout = _timeout_ms(env_name, default)
        if timeout:
            server_settings.setdefault(setting, str(timeout))
    if server_settings:
        connect_args["server_settings"] = server_settings
    poolclass = kwargs.get("poolclass")
    if poolclass is not None and not issubclass(poolclass, QueuePool):
        for key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
            pool_kwargs.pop(key, None)
    return create_async_engine(
        async_url, echo=echo, connect_args=connect_args, **pool_kwargs, **kwargs
    )


def make_session_factory(
    engine: AsyncEngine, *, expire_on_commit: bool = False
) -> async_sessionmaker[AsyncSession]:
    """Build an :class:`async_sessionmaker` scoped to ``engine``."""
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=expire_on_commit
    )


async def dispose_engine(engine: Optional[AsyncEngine]) -> None:
    """Dispose an engine's pool (safe no-op for ``None``)."""
    if engine is not None:
        await engine.dispose()
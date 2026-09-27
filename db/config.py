"""PostgreSQL connection configuration (Phase A).

The URL is resolved from the environment exactly like the legacy JSONB
backend (``storage._database_url_from_environment`` — one source of truth,
already covered by ``tests/test_storage.py``). This module deliberately:

* never logs, prints, or embeds the connection string or password,
* raises :class:`DatabaseConfigError` with a *generic* message when the
  configuration is missing/invalid,
* exposes scheme conversion helpers (``postgresql://`` for psycopg / Alembic
  sync vs ``postgresql+asyncpg://`` for SQLAlchemy async).
"""

from __future__ import annotations

import os
from typing import Optional

from storage import _database_url_from_environment


class DatabaseConfigError(RuntimeError):
    """Clear, secret-free configuration/startup error (fail-fast)."""


def database_url() -> str:
    """Raw PostgreSQL URL from the environment (``""`` when unset)."""
    return _database_url_from_environment()


def strict_kit_roles_enabled() -> bool:
    """Kity bez mapovaných rolí = chyba místo varování? (env ``STRICT_KIT_ROLES``).

    Design §7: kits with no mapped roles are a warning by default; with
    ``STRICT_KIT_ROLES=1`` the bot refuses to start (config is broken).
    """
    return os.getenv("STRICT_KIT_ROLES", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def build_async_database_url(raw: Optional[str] = None) -> str:
    """Normalize any accepted scheme to ``postgresql+asyncpg://``."""
    url = (raw if raw is not None else database_url()).strip()
    if not url:
        return ""
    if url.startswith(("postgresql+asyncpg://", "postgres+asyncpg://")):
        return url
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    raise DatabaseConfigError(
        "Nepodporované schéma v DB připojení (podporováno: postgresql://, postgres://)."
    )


def build_sync_database_url(raw: Optional[str] = None) -> str:
    """Normalize any accepted scheme to plain ``postgresql://`` (psycopg/sync)."""
    url = (raw if raw is not None else database_url()).strip()
    if not url:
        return ""
    if url.startswith(("postgresql+asyncpg://", "postgres+asyncpg://")):
        return "postgresql://" + url.split("://", 1)[1]
    if url.startswith(("postgresql://", "postgres://")):
        return url
    raise DatabaseConfigError(
        "Nepodporované schéma v DB připojení (podporováno: postgresql://, postgres://)."
    )
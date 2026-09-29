"""PostgreSQL connection configuration (Phase A).

The URL is resolved from ``DATABASE_URL`` or assembled from ``DB_HOST`` /
``DB_NAME`` / ``DB_USER`` / ``DB_PASSWORD`` / ``DB_PORT``. This module
deliberately:

* never logs, prints, or embeds the connection string or password,
* raises :class:`DatabaseConfigError` with a *generic* message when the
  configuration is missing/invalid,
* exposes scheme conversion helpers (``postgresql://`` for psycopg / Alembic
  sync vs ``postgresql+asyncpg://`` for SQLAlchemy async).
"""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote


class DatabaseConfigError(RuntimeError):
    """Clear, secret-free configuration/startup error (fail-fast)."""


def _database_url_from_environment() -> str:
    """DATABASE_URL, or one safely assembled from the individual DB_* values."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    host = os.getenv("DB_HOST", "").strip()
    name = os.getenv("DB_NAME", "").strip()
    user = os.getenv("DB_USER", "").strip()
    password = os.getenv("DB_PASSWORD", "")
    port = os.getenv("DB_PORT", "5432").strip() or "5432"
    if not all((host, name, user, password)):
        return ""
    # Passwords may contain @, : or / — without URL encoding part of the
    # password would be parsed as the host.
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{quote(port, safe='')}/{quote(name, safe='')}"
    )


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
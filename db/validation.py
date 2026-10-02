"""Startup validation: connectivity, schema revision, numeric role IDs.

Fail-fast, secret-free checks per docs/MIGRATION_DESIGN.md §7/§13. Error
messages are generic (no connection string, no password, no role IDs);
details go to the log as exception *types* only.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

import storage
from db.config import DatabaseConfigError

log = logging.getLogger("dachshundtiers.db")

MIGRATION_LOCK_KEY = 0x44544D49

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def migration_head() -> str:
    """Latest available Alembic revision (from ``migrations/``)."""
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


def auto_migrate_enabled() -> bool:
    """Run ``alembic upgrade head`` on startup? (env ``AUTO_MIGRATE``, default on).

    Hostings like Bot-Hosting/Pterodactyl only run ``main.py`` and offer no
    shell, so a deploy with a new migration would otherwise never start.
    ``AUTO_MIGRATE=0`` turns it off for setups that migrate separately.
    """
    return os.getenv("AUTO_MIGRATE", "1").strip().lower() not in {"0", "false", "no", "off"}


def upgrade_to_head(sync_url: str) -> tuple[str | None, str]:
    """``alembic upgrade head`` against ``sync_url``; returns (before, after).

    Uses a file-less Alembic config, so ``migrations/env.py`` does not
    re-apply ``alembic.ini`` logging over the bot's own logging setup.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine

    def _current() -> str | None:
        engine = create_engine(sync_url)
        try:
            with engine.connect() as conn:
                exists = conn.execute(text("SELECT to_regclass('alembic_version')")).scalar()
                if exists is None:
                    return None
                row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
                return row[0] if row else None
        finally:
            engine.dispose()

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolation: a URL-encoded password contains '%'.
    cfg.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    # Dvě instance startující naráz (překryv při deployi) nesmí migrovat souběžně:
    # druhá počká na advisory lock a pak už zjistí, že je schéma na head.
    lock_engine = create_engine(sync_url)
    try:
        with lock_engine.connect() as lock_conn:
            lock_conn.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            try:
                before = _current()
                command.upgrade(cfg, "head")
                return before, _current()
            finally:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
                )
    finally:
        lock_engine.dispose()


async def validate_database(engine: AsyncEngine) -> dict:
    """Verify connectivity + migrated schema against the current Alembic head.

    Raises :class:`DatabaseConfigError` with a generic message on any failure.
    Returns ``{"backend", "ok", "schema_revision"}``.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as err:  # noqa: BLE001
        log.exception("Kontrola PostgreSQL spojení selhala.")
        raise DatabaseConfigError(
            "PostgreSQL není dostupné; oprav DATABASE_URL/DB_* nebo počkej a zkus restart."
        ) from err

    head = migration_head()
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).first()
    except ProgrammingError as err:
        log.exception("Schéma PostgreSQL nebylo migrováno (alembic_version chybí).")
        raise DatabaseConfigError(
            "PostgreSQL schéma není migrované — spusť `alembic upgrade head` (viz README)."
        ) from err
    except Exception as err:  # noqa: BLE001
        log.exception("Kontrola PostgreSQL schématu selhala.")
        raise DatabaseConfigError(
            "Nelze ověřit PostgreSQL schéma; podrobnosti jsou v logu."
        ) from err

    if row is None or row[0] != head:
        log.error(
            "Revize schématu %s != očekávaná %s.",
            row[0] if row else "žádná",
            head,
        )
        raise DatabaseConfigError(
            f"PostgreSQL schéma je na revizi {row[0] if row else 'žádné'}, "
            f"očekává se {head}. Spusť `alembic upgrade head`."
        )
    log.info("PostgreSQL schéma je aktuální (revize %s).", head)
    return {"backend": "postgresql", "ok": True, "schema_revision": head}


def validate_configured_role_ids() -> List[int]:
    """Fail-fast check that every ``kit_roles.json`` role ID is numeric.

    Reads the *file* directly (gitignored runtime config; design §7).
    A missing file means no role mapping is configured (logged, not fatal in
    Phase A). Non-numeric or non-int values raise to fail startup — role
    config must never silently degrade. Returns the validated role IDs.
    """
    path = os.path.join(storage.DATA_DIR, "kit_roles.json")
    if not os.path.exists(path):
        log.info("kit_roles.json nenalezeno — role mapping není nakonfigurován (Phase A).")
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as err:
        log.exception("Poškozený kit_roles.json na %s.", path)
        raise DatabaseConfigError(
            "Poškozený kit_roles.json — oprav soubor a restartuj bota."
        ) from err

    problems: List[str] = []
    ids: List[int] = []
    if isinstance(data, dict):
        for kit, tiers in data.items():
            if not isinstance(tiers, dict):
                problems.append(f"{kit}: neočekávaná struktura")
                continue
            for tier, role in tiers.items():
                if isinstance(role, bool):
                    problems.append(f"{kit}/{tier}: {role!r}")
                elif isinstance(role, int):
                    ids.append(role)
                elif isinstance(role, str) and role.strip().isdigit():
                    ids.append(int(role.strip()))
                else:
                    problems.append(f"{kit}/{tier}: {role!r}")
    if problems:
        raise DatabaseConfigError(
            "Nečíselné role ID v kit_roles.json: " + "; ".join(problems[:8])
        )
    log.info("Ověřeno %d role ID v kit_roles.json.", len(ids))
    return sorted(set(ids))
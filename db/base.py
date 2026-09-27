"""SQLAlchemy 2.x declarative base + shared conventions.

Central anchoring for all ``Phase A`` database models:

* deterministic constraint/index naming convention (stable Alembic output),
* UTC-aware ``utcnow()`` helper — never ``datetime.now()`` for persisted data,
* single :class:`Base` that all tables register on.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic names for everything Alembic would otherwise auto-name.
# Explicit ``name=`` on constraints/indexes in models takes precedence over
# these conventions; the convention only covers unnamed objects.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all PostgreSQL models.

    ``metadata.naming_convention`` is fixed once at import time so Alembic
    autogenerate and the initial migration produce identical constraint names.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Timezone-aware UTC ``now()`` for Python-side column defaults.

    Persisted timestamps are always ``TIMESTAMPTZ`` in UTC; Europe/Prague is
    applied only at presentation time.
    """
    return datetime.now(timezone.utc)
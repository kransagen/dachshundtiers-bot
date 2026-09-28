"""Discord ↔ Minecraft identity.

Why a separate table and not a column on ``players``
----------------------------------------------------
A Minecraft account is identified by its **UUID**, never by the (mutable,
case-insensitive) name. The one-to-one link between a Discord user and a
Minecraft account is therefore a *relation* to an entity that has its own
identity rules, not a free-text attribute:

* ``minecraft_accounts.uuid`` is the natural key, format-checked by the
  database so a bad value can never be persisted;
* ``players.minecraft_account_id`` is a UNIQUE FK → exactly one Player per
  Minecraft account and exactly one Minecraft account per Player
  (one-to-one, enforced by the database, not by application code);
* ``minecraft_accounts.name`` is a *mutable display attribute* kept in sync
  when the name changes; it is never used for lookups.

Linking is done with a **one-time, expiring token** (``player_link_tokens``):

* the token is issued in the Discord context (``/link``), where the caller is
  already authenticated as the Discord user;
* the code is shown to the player and must be presented from the Minecraft
  side (a server/plugin/webhook integration) before it can be consumed;
* consumption is single-use and DB-enforced — a second attempt hits the
  partial unique index and fails, so a replayed code is useless even if it
  leaks to a log or a screenshot.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, utcnow

# Canonical Mojang UUID: 8-4-4-4-12 lowercase hex. Enforced by a CHECK so a
# malformed identifier can never reach the database, whatever the caller does.
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_UUID_RE = re.compile(UUID_PATTERN)

# Reasons a link token can be rejected. Kept as a CHECK so the audit trail
# cannot contain a value the code does not know how to read.
TOKEN_REJECTION_REASONS = ("expired", "superseded", "wrong_uuid", "manual")


def normalize_uuid(value: str) -> str:
    """Normalize a user-supplied Mojang UUID to canonical lowercase form.

    Accepts the dashed form players normally paste as well as the undashed
    32-character form. Returns ``""`` when the value is not a UUID, so
    callers must treat an empty result as a validation error rather than
    persisting it.
    """
    raw = (value or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    if "-" not in raw and len(raw) == 32:
        raw = "-".join(
            (raw[0:8], raw[8:12], raw[12:16], raw[16:20], raw[20:32])
        )
    return raw if _UUID_RE.match(raw) else ""


class MinecraftAccount(Base):
    """One real Minecraft account, keyed by its UUID."""

    __tablename__ = "minecraft_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    uuid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

    __table_args__ = (
        CheckConstraint(f"uuid ~ '{UUID_PATTERN}'", name="uuid_format"),
    )


class PlayerLinkToken(Base):
    """One-time, expiring proof that a Discord user owns a Minecraft UUID.

    A row is created by ``/link`` and *must* be consumed exactly once. Rows
    that were never used are kept (with ``consumed_at IS NULL``) so the audit
    trail shows how many link attempts happened; ``uq_link_token_single_use``
    makes a second consumption of the same row impossible.
    """

    __tablename__ = "player_link_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    # Uniqueness is declared once, in __table_args__, so there is exactly one
    # UniqueConstraint object in the metadata (a column-level ``unique=True``
    # would add a second, differently named one and make autogenerate emit a
    # spurious drop/create).
    code: Mapped[str] = mapped_column(Text, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    minecraft_account_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("minecraft_accounts.id")
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        # Fast "which live token does this Discord user have" lookup.
        Index("ix_link_token_player_live", "player_id", postgresql_where=text("consumed_at IS NULL")),
        # An expired token must never look consumable; DB-level guarantee.
        CheckConstraint("expires_at > issued_at", name="expiry_after_issue"),
        # A consumed token must record *what* was linked and *why* it was
        # rejected, and the two are mutually exclusive.
        CheckConstraint(
            "(consumed_at IS NULL AND rejection_reason IS NULL)"
            " OR (consumed_at IS NOT NULL AND rejection_reason IS NULL"
            " AND minecraft_account_id IS NOT NULL)"
            " OR (consumed_at IS NOT NULL AND rejection_reason IS NOT NULL)",
            name="consumed_state",
        ),
        CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('expired', 'superseded', 'wrong_uuid', 'manual')",
            name="rejection_reason",
        ),
        UniqueConstraint("code", name="uq_link_token_code"),
    )

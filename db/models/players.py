"""Players, kits, tier definitions, role mapping and tier state models.

Core of the design invariant: ``player_current_tiers`` is a mirror of what
Discord has *confirmed* (design §5) — it has no independent authority.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, utcnow

LADDER_SOURCE_VALUES = ("migration", "discord")


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    discord_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    ign: Mapped[str] = mapped_column(Text, nullable=False)
    # One-to-one with a Minecraft account (see db/models/identity.py). The
    # UNIQUE is what makes the link one-to-one: without it two Players could
    # point at the same UUID, and "who owns this Minecraft account" would
    # have two answers. NULL = never linked.
    minecraft_account_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("minecraft_accounts.id"), unique=True
    )
    source: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'discord'"),
    )
    # Kdy hráč sám potvrdil, že IGN je jeho (/linkign, admin /linkdiscord).
    # NULL = záznam vznikl jen z přezdívky na serveru / importu – do fronty
    # ani do HT3+ ticketu takový hráč nesmí, dokud se nepropojí.
    ign_linked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
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
        Index(
            "uq_players_discord_id",
            "discord_id",
            unique=True,
            postgresql_where=text("discord_id IS NOT NULL"),
        ),
        Index("uq_players_ign", text("lower(ign)"), unique=True),
        CheckConstraint(
            "source IN ('migration', 'discord')",
            name="source",
        ),
    )


class Kit(Base):
    __tablename__ = "kits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )


class TierDefinition(Base):
    __tablename__ = "tier_definitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    rank: Mapped[Optional[int]] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_retired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    retired_of_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('ladder', 'tournament', 'retired', 'virtual')",
            name="kind",
        ),
    )


class KitRole(Base):
    __tablename__ = "kit_roles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    tier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id"), nullable=False
    )
    discord_role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
        UniqueConstraint("kit_id", "tier_id", name="uq_kit_roles_kit_tier"),
        UniqueConstraint("discord_role_id", name="uq_kit_roles_role"),
    )


class PlayerCurrentTier(Base):
    __tablename__ = "player_current_tiers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    tier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id"), nullable=False
    )
    discord_role_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    sync_run_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("sync_runs.id"))
    result_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("results.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=utcnow,
    )

    __table_args__ = (
        UniqueConstraint("player_id", "kit_id", name="uq_cur_tier_player_kit"),
        Index("ix_cur_tier_kit", "kit_id", "tier_id"),
        Index("ix_cur_tier_role", "discord_role_id"),
        CheckConstraint(
            "source IN ('discord_sync', 'promotion', 'manual', 'retire')",
            name="source",
        ),
    )


class TierHistory(Base):
    __tablename__ = "tier_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    tier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id"), nullable=False
    )
    previous_tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    result_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("results.id"))
    sync_run_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("sync_runs.id"))
    actor_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    actor_name: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_th_player_kit_time",
            "player_id",
            "kit_id",
            desc("changed_at"),
        ),
        Index("ix_th_kit", "kit_id"),
        CheckConstraint(
            "source IN ('promotion', 'discord_sync', 'migration', 'manual', 'rollback', 'retire')",
            name="source",
        ),
    )

PEAK_REASONS = ("days", "wins", "retire", "manual")


class PlayerPeakTier(Base):
    """Nejvyšší trvale dosažený tier hráče v kitu (peak) – nikdy se nemaže.

    Jeden řádek na (hráč, kit); při dosažení vyššího peaku se přepíše jen
    směrem nahoru. ``reason`` říká, čím hráč podmínku splnil.
    """

    __tablename__ = "player_peak_tiers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    kit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kits.id"), nullable=False
    )
    tier_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    achieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
        UniqueConstraint("player_id", "kit_id", name="uq_peak_player_kit"),
        CheckConstraint(
            "reason IN ('days', 'wins', 'retire', 'manual')",
            name="reason",
        ),
    )

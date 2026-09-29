"""Sync/audit/outbox/config models: the recoverability wedge + audit trail."""

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
    text,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    discord_role_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # M7 audit fix: when a row was actually claimed (status -> in_progress),
    # distinct from created_at (enqueue time). Staleness/reclaim decisions
    # must be based on "how long has this claim been held", not "how old is
    # the event" — otherwise a merely-backlogged-then-claimed event is
    # immediately eligible for a second consumer to steal it out from under
    # active processing.
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_outbox_pending",
            "status",
            "created_at",
            postgresql_where=text("status IN ('pending', 'in_progress')"),
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'failed', 'dead_letter')",
            name="status",
        ),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    triggered_by_name: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="running"
    )
    summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        CheckConstraint(
            "mode IN ('preview', 'apply', 'automatic', 'rollback', 'observe')",
            name="mode",
        ),
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'partial')",
            name="status",
        ),
    )


class SyncAction(Base):
    __tablename__ = "sync_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sync_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sync_runs.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    player_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("players.id")
    )
    kit_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("kits.id"))
    tier_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("tier_definitions.id")
    )
    discord_role_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    member_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    anomaly_category: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="applied"
    )
    details: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_sync_actions_run", "sync_run_id"),
        Index("ix_sync_actions_anom", "anomaly_category"),
        CheckConstraint(
            "status IN ('applied', 'skipped', 'failed', 'anomaly')",
            name="status",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    actor_name: Mapped[Optional[str]] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(Text)
    entity_id: Mapped[Optional[str]] = mapped_column(Text)
    details: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_actor", "actor_id"),
    )


class BotConfig(Base):
    __tablename__ = "bot_config"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MigrationImportIssue(Base):
    __tablename__ = "migration_import_issues"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_key: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="open"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_migration_issues_category", "category"),
        CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')",
            name="status",
        ),
    )
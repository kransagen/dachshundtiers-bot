"""Sync-run, sync-action, audit-log, bot-config and migration-issue repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AuditLog, BotConfig, MigrationImportIssue, SyncAction, SyncRun

SYNC_RUN_RUNNING = "running"
SYNC_RUN_SUCCESS = "success"
SYNC_RUN_FAILED = "failed"
SYNC_RUN_PARTIAL = "partial"

SYNC_ACTION_APPLIED = "applied"
SYNC_ACTION_SKIPPED = "skipped"
SYNC_ACTION_FAILED = "failed"
SYNC_ACTION_ANOMALY = "anomaly"


class SyncRunRepository:
    """Bookends of one /sync invocation (preview/apply/automatic/rollback)."""

    async def start(
        self,
        session: AsyncSession,
        *,
        command: str,
        mode: str,
        triggered_by: Optional[int] = None,
        triggered_by_name: Optional[str] = None,
    ) -> SyncRun:
        row = SyncRun(
            command=command,
            mode=mode,
            triggered_by=triggered_by,
            triggered_by_name=triggered_by_name,
            status=SYNC_RUN_RUNNING,
        )
        session.add(row)
        await session.flush()
        return row

    async def finish(
        self,
        session: AsyncSession,
        *,
        sync_run_id: int,
        status: str,
        summary: Optional[dict] = None,
        finished_at: Optional[datetime] = None,
    ) -> Optional[SyncRun]:
        row = await self.get(session, sync_run_id)
        if row is None:
            return None
        row.status = status
        if summary is not None:
            row.summary = summary
        row.finished_at = finished_at or datetime.now(timezone.utc)
        await session.flush()
        return row

    async def get(self, session: AsyncSession, sync_run_id: int) -> Optional[SyncRun]:
        return await session.get(SyncRun, sync_run_id)

    async def list(
        self, session: AsyncSession, *, command: Optional[str] = None, limit: int = 50
    ) -> list[SyncRun]:
        stmt = select(SyncRun)
        if command is not None:
            stmt = stmt.where(SyncRun.command == command)
        result = await session.execute(
            stmt.order_by(SyncRun.started_at.desc()).limit(limit)
        )
        return list(result.scalars())


class SyncActionRepository:
    """Per-member outcome rows of a sync run (incl. anomalies)."""

    async def record(
        self,
        session: AsyncSession,
        *,
        sync_run_id: int,
        action_type: str,
        player_id: Optional[int] = None,
        kit_id: Optional[int] = None,
        tier_id: Optional[int] = None,
        discord_role_id: Optional[int] = None,
        member_id: Optional[int] = None,
        anomaly_category: Optional[str] = None,
        status: str = SYNC_ACTION_APPLIED,
        details: Optional[dict] = None,
    ) -> SyncAction:
        row = SyncAction(
            sync_run_id=sync_run_id,
            action_type=action_type,
            player_id=player_id,
            kit_id=kit_id,
            tier_id=tier_id,
            discord_role_id=discord_role_id,
            member_id=member_id,
            anomaly_category=anomaly_category,
            status=status,
            details=details,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_for_run(
        self, session: AsyncSession, *, sync_run_id: int
    ) -> list[SyncAction]:
        result = await session.execute(
            select(SyncAction)
            .where(SyncAction.sync_run_id == sync_run_id)
            .order_by(SyncAction.id)
        )
        return list(result.scalars())


class AuditRepository:
    """Append-only audit trail. No update/delete methods are exposed."""

    async def append(
        self,
        session: AsyncSession,
        *,
        action: str,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> AuditLog:
        row = AuditLog(
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
        )
        session.add(row)
        await session.flush()
        return row

    async def list(
        self,
        session: AsyncSession,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        stmt = select(AuditLog)
        if entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        result = await session.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit))
        return list(result.scalars())


class BotConfigRepository:
    """Key -> JSONB value store (server config; not secrets)."""

    async def get(
        self, session: AsyncSession, key: str, default: Any = None
    ) -> Any:
        row = await session.get(BotConfig, key)
        return row.value if row is not None else default

    async def set(self, session: AsyncSession, key: str, value: dict) -> None:
        await session.execute(
            pg_insert(BotConfig)
            .values(key=key, value=value)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": value,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await session.flush()

    async def merge(self, session: AsyncSession, key: str, patch: dict) -> None:
        """Atomically merge ``patch`` into the stored JSONB object (``||``).

        One statement, so two concurrent merges of different sub-keys both
        survive (a read-modify-write in Python would lose one of them).
        """
        await session.execute(
            pg_insert(BotConfig)
            .values(key=key, value=patch)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": BotConfig.value.op("||")(pg_insert(BotConfig).excluded.value),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await session.flush()


class MigrationIssueRepository:
    """Unresolvable records found during JSON import (report, never guessed)."""

    async def record(
        self,
        session: AsyncSession,
        *,
        category: str,
        source_file: str,
        reason: str,
        payload: dict,
        source_key: Optional[str] = None,
    ) -> MigrationImportIssue:
        row = MigrationImportIssue(
            category=category,
            source_file=source_file,
            source_key=source_key,
            reason=reason,
            payload=payload,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_open(
        self, session: AsyncSession, *, category: Optional[str] = None, limit: int = 200
    ) -> list[MigrationImportIssue]:
        stmt = select(MigrationImportIssue).where(MigrationImportIssue.status == "open")
        if category is not None:
            stmt = stmt.where(MigrationImportIssue.category == category)
        result = await session.execute(stmt.order_by(MigrationImportIssue.id).limit(limit))
        return list(result.scalars())
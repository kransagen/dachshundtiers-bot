"""Discord -> PostgreSQL mirror sync (Phase C, item 2).

One ``/sync discord`` run observes the guild's CURRENT Discord roles and mirrors
them into ``player_current_tiers``. This service performs the observation only:
it NEVER adds/removes/changes Discord roles, and it never writes a tier from
ambiguous input (multiple mapped roles on one member) or for members the
database does not know (unknown players stay anomalous, never fabricated).
Everything is recorded in SyncRun/SyncAction rows + the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.repositories.kits import KitRepository, KitRoleRepository
from db.repositories.players import PlayerRepository
from db.repositories.sync_audit import (
    SYNC_ACTION_ANOMALY,
    SYNC_ACTION_APPLIED,
    SYNC_ACTION_FAILED,
    SYNC_RUN_FAILED,
    SYNC_RUN_PARTIAL,
    SYNC_RUN_SUCCESS,
    AuditRepository,
    SyncActionRepository,
    SyncRunRepository,
)
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction
from db.services.tier_mirror import (
    ANOMALY_UNKNOWN_ROLES,
    ClassificationResult,
    MirrorService,
    classify_member_roles,
)

SYNC_ANOMALY_UNKNOWN_PLAYER = "unknown_player"

OBSERVE_SOURCE = "discord_sync"
OBSERVE_REASON = "/sync discord observe — mirror only"


@dataclass(frozen=True)
class DiscordSyncOutcome:
    sync_run_id: int
    status: str
    scanned_members: int
    observations_applied: int
    anomalies: int
    unknown_members: int
    failed_members: int
    unknown_roles: tuple[int, ...]


class DiscordSyncService:
    """Observe guild members' current roles; mirror into PG. Read-only on Discord."""

    def __init__(
        self,
        mirrors: Optional[MirrorServiceRepository] = None,
        kits: Optional[KitRepository] = None,
        kit_roles: Optional[KitRoleRepository] = None,
        players: Optional[PlayerRepository] = None,
        sync_runs: Optional[SyncRunRepository] = None,
        sync_actions: Optional[SyncActionRepository] = None,
        audit: Optional[AuditRepository] = None,
    ) -> None:
        self._mirror_service = MirrorService(
            mirrors=mirrors, kits=kits, kit_roles=kit_roles
        )
        self._kits = kits or KitRepository()
        self._kit_roles = kit_roles or KitRoleRepository()
        self._players = players or PlayerRepository()
        self._sync_runs = sync_runs or SyncRunRepository()
        self._sync_actions = sync_actions or SyncActionRepository()
        self._audit = audit or AuditRepository()

    async def sync_guild(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        members,
        observed_at: Optional[datetime] = None,
        triggered_by: Optional[int] = None,
        triggered_by_name: Optional[str] = None,
        command: str = "/sync discord",
        mode: str = "observe",
    ) -> DiscordSyncOutcome:
        """Mirror one guild scan into PG. ``members`` are objects with
        ``.id`` (int) and ``.role_ids`` (iterable of ints)."""
        observed_at = observed_at or datetime.now(timezone.utc)
        scanned = 0
        applied = 0
        anomalies = 0
        unknown_members = 0
        failed_members = 0
        unknown_roles: set[int] = set()

        async with transaction(session_factory) as session:
            snapshot = await self._kit_roles.role_snapshot(session)
            kits = await self._kits.list(session, only_active=True)
            kit_ids = {k.key: k.id for k in kits}
            kit_keys = {k.id: k.key for k in kits}
            run = await self._sync_runs.start(
                session,
                command=command,
                mode=mode,
                triggered_by=triggered_by,
                triggered_by_name=triggered_by_name,
            )

            for member in members:
                scanned += 1
                try:
                    member_id = int(member.id)
                    role_ids = {int(role_id) for role_id in member.role_ids}
                    member_outcome = await self._sync_member(
                        session,
                        run_id=run.id,
                        member_id=member_id,
                        role_ids=role_ids,
                        snapshot=snapshot,
                        kit_ids=kit_ids,
                        kit_keys=kit_keys,
                        observed_at=observed_at,
                    )
                    applied += member_outcome.applied
                    anomalies += member_outcome.anomalies
                    if member_outcome.unknown:
                        unknown_members += 1
                    unknown_roles.update(member_outcome.unknown_roles)
                except Exception as exc:  # noqa: BLE001 — one member never kills the run
                    failed_members += 1
                    await self._sync_actions.record(
                        session,
                        sync_run_id=run.id,
                        action_type="observe",
                        member_id=member_id,
                        status=SYNC_ACTION_FAILED,
                        details={"error": str(exc)[:2000]},
                    )

            if failed_members == 0:
                status = SYNC_RUN_SUCCESS
            elif failed_members >= scanned:
                status = SYNC_RUN_FAILED
            else:
                status = SYNC_RUN_PARTIAL

            summary = {
                "scanned": scanned,
                "applied": applied,
                "anomalies": anomalies,
                "unknown_members": unknown_members,
                "failed_members": failed_members,
                "unknown_roles": sorted(unknown_roles),
            }
            await self._sync_runs.finish(
                session, sync_run_id=run.id, status=status, summary=summary
            )
            await self._audit.append(
                session,
                action="sync_discord_observe",
                actor_id=triggered_by,
                actor_name=triggered_by_name,
                entity_type="sync_run",
                entity_id=str(run.id),
                details=summary,
            )
            return DiscordSyncOutcome(
                sync_run_id=run.id,
                status=status,
                scanned_members=scanned,
                observations_applied=applied,
                anomalies=anomalies,
                unknown_members=unknown_members,
                failed_members=failed_members,
                unknown_roles=tuple(sorted(unknown_roles)),
            )

    async def _sync_member(
        self,
        session: AsyncSession,
        *,
        run_id: int,
        member_id: int,
        role_ids: set[int],
        snapshot: dict[int, tuple[int, int]],
        kit_ids: dict[str, int],
        kit_keys: dict[int, str],
        observed_at: datetime,
    ) -> "_MemberOutcome":
        player = await self._players.get_by_discord_id(session, member_id)
        if player is None:
            await self._sync_actions.record(
                session,
                sync_run_id=run_id,
                action_type="observe",
                member_id=member_id,
                anomaly_category=SYNC_ANOMALY_UNKNOWN_PLAYER,
                status=SYNC_ACTION_ANOMALY,
                details={"role_count": len(role_ids)},
            )
            return _MemberOutcome(applied=0, anomalies=1, unknown=True)

        classification = classify_member_roles(snapshot, role_ids, kit_ids, kit_keys)
        anomalies = 0
        if classification.unknown_role_ids:
            await self._sync_actions.record(
                session,
                sync_run_id=run_id,
                action_type="observe",
                player_id=player.id,
                member_id=member_id,
                anomaly_category=ANOMALY_UNKNOWN_ROLES,
                status=SYNC_ACTION_ANOMALY,
                details={"unknown_role_ids": list(classification.unknown_role_ids)},
            )
            anomalies += 1

        for obs in classification.observations:
            if obs.tier_id is None:
                await self._sync_actions.record(
                    session,
                    sync_run_id=run_id,
                    action_type="observe",
                    player_id=player.id,
                    member_id=member_id,
                    kit_id=obs.kit_id,
                    anomaly_category=obs.anomaly,
                    status=SYNC_ACTION_ANOMALY,
                    details={"tier_id": None},
                )
                anomalies += 1

        clean = [o for o in classification.observations if o.tier_id is not None]
        applied = 0
        if clean:
            mirror_results = await self._mirror_service.apply_observations(
                session,
                player_id=player.id,
                classification=ClassificationResult(
                    observations=tuple(clean), unknown_role_ids=()
                ),
                observed_at=observed_at,
                source=OBSERVE_SOURCE,
                sync_run_id=run_id,
                actor_id=None,
                reason=OBSERVE_REASON,
            )
            for result, obs in zip(mirror_results, clean):
                await self._sync_actions.record(
                    session,
                    sync_run_id=run_id,
                    action_type="observe",
                    player_id=player.id,
                    member_id=member_id,
                    kit_id=obs.kit_id,
                    tier_id=obs.tier_id,
                    discord_role_id=obs.discord_role_id,
                    status=SYNC_ACTION_APPLIED,
                    details={"tier_changed": result.tier_changed},
                )
            applied = len(mirror_results)

        return _MemberOutcome(
            applied=applied,
            anomalies=anomalies,
            unknown_roles=tuple(classification.unknown_role_ids),
        )


@dataclass(frozen=True)
class _MemberOutcome:
    applied: int
    anomalies: int
    unknown: bool = False
    unknown_roles: tuple[int, ...] = ()
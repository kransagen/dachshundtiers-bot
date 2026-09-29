"""Discord -> PostgreSQL mirror sync.

Discord is the authority for current tiers. One run observes the guild's
CURRENT Discord roles and mirrors them into ``player_current_tiers``. The
service NEVER adds/removes/changes Discord roles and never writes a tier from
ambiguous input (multiple mapped roles for one kit on one member).

Used by:

* the automatic reconciliation (startup + hourly) — ``create_missing_players``
  is off, so members the database does not know stay anomalies;
* ``/sync import-discord`` — ``create_missing_players`` is on (a member with a
  tier role but no DB record is created from their display name), and
  ``dry_run`` gives a preview that rolls the whole run back.

Each member is processed in its own SAVEPOINT, so one failing member never
aborts the surrounding PostgreSQL transaction for the rest of the server.
Only real changes and anomalies are written to ``sync_actions``; an unchanged
re-observation leaves no audit row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Player, PlayerCurrentTier, TierDefinition
from db.repositories.kits import KitRepository, KitRoleRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
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
from db.services.tier_mirror import (
    ANOMALY_MULTIPLE_TIERS,
    ANOMALY_UNKNOWN_ROLES,
    ClassificationResult,
    MirrorService,
    classify_member_roles,
)

SYNC_ANOMALY_UNKNOWN_PLAYER = "unknown_player"
SYNC_ANOMALY_IDENTITY_CONFLICT = "identity_conflict"
SYNC_ANOMALY_MISSING_ON_DISCORD = "missing_on_discord"

CHANGE_PLAYER_CREATED = "player_created"
CHANGE_TIER_ADDED = "tier_added"
CHANGE_TIER_CHANGED = "tier_changed"

OBSERVE_SOURCE = "discord_sync"
OBSERVE_REASON = "Discord → PostgreSQL mirror"


@dataclass(frozen=True)
class SyncChange:
    """One reportable line of a run (a change or an anomaly)."""

    member_id: int
    kind: str
    kit_key: Optional[str] = None
    old_tier: Optional[str] = None
    new_tier: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class DiscordSyncOutcome:
    sync_run_id: Optional[int]
    status: str
    scanned_members: int
    observations_applied: int
    anomalies: int
    unknown_members: int
    failed_members: int
    unknown_roles: tuple[int, ...]
    created_players: int = 0
    tier_changes: int = 0
    dry_run: bool = False
    changes: tuple[SyncChange, ...] = ()


def member_role_ids(member) -> set[int]:
    """Role IDs of a guild member (its ``roles`` list) or of a test view."""
    explicit = getattr(member, "role_ids", None)
    if explicit is not None:
        return {int(r) for r in explicit}
    return {int(r.id) for r in getattr(member, "roles", ())}


def _member_name(member) -> str:
    for attr in ("display_name", "name"):
        value = getattr(member, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass
class _MemberOutcome:
    applied: int = 0
    anomalies: int = 0
    unknown: bool = False
    created: bool = False
    tier_changes: int = 0
    unknown_roles: tuple[int, ...] = ()
    changes: list[SyncChange] = field(default_factory=list)


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
        command: str = "reconcile",
        mode: str = "observe",
        create_missing_players: bool = False,
        dry_run: bool = False,
    ) -> DiscordSyncOutcome:
        """Mirror one guild scan into PG.

        ``members`` are guild member objects (anything with ``.id`` and
        ``.roles``/``.role_ids``). With ``dry_run`` the whole run —
        including the SyncRun row — is rolled back and only the in-memory
        ``changes`` are returned.
        """
        observed_at = observed_at or datetime.now(timezone.utc)
        members = list(members)
        scanned = applied = anomalies = unknown_members = failed_members = 0
        created = tier_changes = 0
        unknown_roles: set[int] = set()
        changes: list[SyncChange] = []

        async with session_factory() as session:
            trans = await session.begin()
            try:
                snapshot = await self._kit_roles.role_snapshot(session)
                kits = await self._kits.list(session, only_active=True)
                kit_ids = {k.key: k.id for k in kits}
                kit_keys = {k.id: k.key for k in kits}
                tier_codes = dict(
                    (await session.execute(
                        select(TierDefinition.id, TierDefinition.code)
                    )).all()
                )
                players_by_did = await self._load_players(session, members)
                mirrors = await self._load_mirrors(
                    session, [p.id for p in players_by_did.values()]
                )
                run = await self._sync_runs.start(
                    session,
                    command=command,
                    mode=mode,
                    triggered_by=triggered_by,
                    triggered_by_name=triggered_by_name,
                )

                for member in members:
                    scanned += 1
                    member_id = int(member.id)
                    try:
                        async with session.begin_nested():
                            outcome = await self._sync_member(
                                session,
                                run_id=run.id,
                                member=member,
                                member_id=member_id,
                                player=players_by_did.get(member_id),
                                mirrors=mirrors,
                                snapshot=snapshot,
                                kit_ids=kit_ids,
                                kit_keys=kit_keys,
                                tier_codes=tier_codes,
                                observed_at=observed_at,
                                create_missing_players=create_missing_players,
                            )
                    except Exception as exc:  # noqa: BLE001 — one member never kills the run
                        failed_members += 1
                        changes.append(
                            SyncChange(member_id=member_id, kind="failed", detail=str(exc)[:200])
                        )
                        await self._sync_actions.record(
                            session,
                            sync_run_id=run.id,
                            action_type="observe",
                            member_id=member_id,
                            status=SYNC_ACTION_FAILED,
                            details={"error": str(exc)[:2000]},
                        )
                        continue
                    applied += outcome.applied
                    anomalies += outcome.anomalies
                    created += int(outcome.created)
                    tier_changes += outcome.tier_changes
                    unknown_members += int(outcome.unknown)
                    unknown_roles.update(outcome.unknown_roles)
                    changes.extend(outcome.changes)

                if failed_members == 0:
                    status = SYNC_RUN_SUCCESS
                elif failed_members >= scanned:
                    status = SYNC_RUN_FAILED
                else:
                    status = SYNC_RUN_PARTIAL

                summary = {
                    "scanned": scanned,
                    "applied": applied,
                    "tier_changes": tier_changes,
                    "created_players": created,
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
                run_id = run.id
            except BaseException:
                await trans.rollback()
                raise
            if dry_run:
                await trans.rollback()
                run_id = None
            else:
                await trans.commit()

        return DiscordSyncOutcome(
            sync_run_id=run_id,
            status=status,
            scanned_members=scanned,
            observations_applied=applied,
            anomalies=anomalies,
            unknown_members=unknown_members,
            failed_members=failed_members,
            unknown_roles=tuple(sorted(unknown_roles)),
            created_players=created,
            tier_changes=tier_changes,
            dry_run=dry_run,
            changes=tuple(changes),
        )

    async def _load_players(self, session: AsyncSession, members) -> dict[int, Player]:
        ids = {int(m.id) for m in members}
        if not ids:
            return {}
        rows = await session.execute(select(Player).where(Player.discord_id.in_(ids)))
        return {int(p.discord_id): p for p in rows.scalars()}

    async def _load_mirrors(
        self, session: AsyncSession, player_ids: list[int]
    ) -> dict[int, dict[int, int]]:
        """player_id -> {kit_id: tier_id} of the current mirror."""
        if not player_ids:
            return {}
        rows = await session.execute(
            select(
                PlayerCurrentTier.player_id,
                PlayerCurrentTier.kit_id,
                PlayerCurrentTier.tier_id,
            ).where(PlayerCurrentTier.player_id.in_(player_ids))
        )
        out: dict[int, dict[int, int]] = {}
        for player_id, kit_id, tier_id in rows.all():
            out.setdefault(player_id, {})[kit_id] = tier_id
        return out

    async def _anomaly(
        self,
        session: AsyncSession,
        out: _MemberOutcome,
        *,
        run_id: int,
        member_id: int,
        category: str,
        player_id: Optional[int] = None,
        kit_id: Optional[int] = None,
        kit_key: Optional[str] = None,
        details: Optional[dict] = None,
        old_tier: Optional[str] = None,
    ) -> None:
        await self._sync_actions.record(
            session,
            sync_run_id=run_id,
            action_type="observe",
            player_id=player_id,
            member_id=member_id,
            kit_id=kit_id,
            anomaly_category=category,
            status=SYNC_ACTION_ANOMALY,
            details=details,
        )
        out.anomalies += 1
        out.changes.append(
            SyncChange(member_id=member_id, kind=category, kit_key=kit_key, old_tier=old_tier)
        )

    async def _sync_member(
        self,
        session: AsyncSession,
        *,
        run_id: int,
        member,
        member_id: int,
        player: Optional[Player],
        mirrors: dict[int, dict[int, int]],
        snapshot: dict[int, tuple[int, int]],
        kit_ids: dict[str, int],
        kit_keys: dict[int, str],
        tier_codes: dict[int, str],
        observed_at: datetime,
        create_missing_players: bool,
    ) -> _MemberOutcome:
        out = _MemberOutcome()
        role_ids = member_role_ids(member)
        classification = classify_member_roles(snapshot, role_ids, kit_ids, kit_keys)
        clean = [o for o in classification.observations if o.tier_id is not None]

        if player is None:
            if not clean:
                # Member without any tier role – nothing to mirror, not an anomaly.
                return out
            if not create_missing_players:
                await self._anomaly(
                    session, out, run_id=run_id, member_id=member_id,
                    category=SYNC_ANOMALY_UNKNOWN_PLAYER,
                    details={"role_count": len(role_ids)},
                )
                out.unknown = True
                return out
            try:
                _claim, player = await self._players.claim_discord_id(
                    session, discord_id=member_id, ign=_member_name(member)
                )
            except PlayerIdentityError as err:
                await self._anomaly(
                    session, out, run_id=run_id, member_id=member_id,
                    category=SYNC_ANOMALY_IDENTITY_CONFLICT,
                    details={"error": str(err)[:500]},
                )
                return out
            out.created = True
            out.changes.append(
                SyncChange(member_id=member_id, kind=CHANGE_PLAYER_CREATED, detail=player.ign)
            )

        if classification.unknown_role_ids:
            out.unknown_roles = tuple(classification.unknown_role_ids)

        current = mirrors.get(player.id, {})
        for obs in classification.observations:
            if obs.tier_id is not None:
                continue
            if obs.anomaly == ANOMALY_MULTIPLE_TIERS:
                await self._anomaly(
                    session, out, run_id=run_id, member_id=member_id,
                    category=obs.anomaly, player_id=player.id,
                    kit_id=obs.kit_id, kit_key=obs.kit_key,
                )
            elif obs.kit_id in current:
                # DB has a tier for this kit, Discord has no role → report only.
                await self._anomaly(
                    session, out, run_id=run_id, member_id=member_id,
                    category=SYNC_ANOMALY_MISSING_ON_DISCORD, player_id=player.id,
                    kit_id=obs.kit_id, kit_key=obs.kit_key,
                    old_tier=tier_codes.get(current[obs.kit_id]),
                    details={"db_tier_id": current[obs.kit_id]},
                )

        if not clean:
            return out

        results = await self._mirror_service.apply_observations(
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
        out.applied = len(results)
        for result, obs in zip(results, clean):
            if not result.tier_changed:
                continue
            out.tier_changes += 1
            kind = CHANGE_TIER_ADDED if result.first_observation else CHANGE_TIER_CHANGED
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
                details={"tier_changed": True, "change": kind},
            )
            out.changes.append(
                SyncChange(
                    member_id=member_id,
                    kind=kind,
                    kit_key=obs.kit_key,
                    old_tier=tier_codes.get(result.previous_tier_id)
                    if not result.first_observation else None,
                    new_tier=tier_codes.get(obs.tier_id),
                )
            )
        return out


__all__ = [
    "ANOMALY_UNKNOWN_ROLES",
    "CHANGE_PLAYER_CREATED",
    "CHANGE_TIER_ADDED",
    "CHANGE_TIER_CHANGED",
    "DiscordSyncOutcome",
    "DiscordSyncService",
    "SYNC_ANOMALY_IDENTITY_CONFLICT",
    "SYNC_ANOMALY_MISSING_ON_DISCORD",
    "SYNC_ANOMALY_UNKNOWN_PLAYER",
    "SyncChange",
    "member_role_ids",
]

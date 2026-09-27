"""Tier mirror service — pure classification + mirror observation application.

``classify_member_roles`` is a pure function (no DB, no Discord) mapping a
member's Discord role IDs to (kit -> tier) observations via the kit-role
snapshot. It has no way to write anything; the MirrorServiceRepository applies
observations into ``player_current_tiers`` (a Discord-confirmed mirror), never
the other direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories.kits import KitRepository, KitRoleRepository
from db.repositories.tiers import MirrorServiceRepository, ObservationResult

ANOMALY_UNKNOWN_ROLES = "unknown_roles"
ANOMALY_MULTIPLE_TIERS = "multiple_tier_roles"
ANOMALY_MISSING_TIER = "missing_tier"


@dataclass(frozen=True)
class KitObservation:
    kit_id: int
    kit_key: str
    tier_id: Optional[int]
    discord_role_id: Optional[int]
    anomaly: Optional[str]


@dataclass(frozen=True)
class ClassificationResult:
    observations: tuple[KitObservation, ...]
    unknown_role_ids: tuple[int, ...]


def classify_member_roles(
    role_snapshot: dict[int, tuple[int, int]],
    member_role_ids: set[int],
    kit_ids: dict[str, int],
    kit_keys: dict[int, str],
) -> ClassificationResult:
    """Pure: member roles -> per-kit observations.

    A kit observed via exactly one mapped role yields a clean observation; via
    multiple roles it is an ``ANOMALY_MULTIPLE_TIERS`` (tier_id None — the
    mirror is never written from ambiguous input); mapped roles present in the
    snapshot but absent on the member produce ``ANOMALY_MISSING_TIER``
    observations (report-only in Phase B; clearing needs an explicit later
    phase). Unmapped member roles are returned as unknown ids.
    """
    by_kit: dict[int, list[tuple[int, int]]] = {}
    seen_roles: set[int] = set()
    for role_id, (kit_id, tier_id) in role_snapshot.items():
        if role_id in member_role_ids:
            by_kit.setdefault(kit_id, []).append((role_id, tier_id))
            seen_roles.add(role_id)

    observations: list[KitObservation] = []
    for kit_id, roles in by_kit.items():
        key = kit_keys.get(kit_id, f"kit-{kit_id}")
        if len(roles) > 1:
            observations.append(
                KitObservation(
                    kit_id=kit_id,
                    kit_key=key,
                    tier_id=None,
                    discord_role_id=None,
                    anomaly=ANOMALY_MULTIPLE_TIERS,
                )
            )
        else:
            role_id, tier_id = roles[0]
            observations.append(
                KitObservation(
                    kit_id=kit_id,
                    kit_key=key,
                    tier_id=tier_id,
                    discord_role_id=role_id,
                    anomaly=None,
                )
            )

    all_kit_ids = set(kit_ids.values())
    for kit_id in all_kit_ids - set(by_kit.keys()):
        observations.append(
            KitObservation(
                kit_id=kit_id,
                kit_key=kit_keys.get(kit_id, f"kit-{kit_id}"),
                tier_id=None,
                discord_role_id=None,
                anomaly=ANOMALY_MISSING_TIER,
            )
        )

    unknown = sorted(member_role_ids - seen_roles)
    return ClassificationResult(
        observations=tuple(sorted(observations, key=lambda o: o.kit_id)),
        unknown_role_ids=tuple(unknown),
    )


class MirrorService:
    """Applies Discord-observed tier changes into the mirror via one service
    call; anomaly observations are returned to the caller un-applied."""

    def __init__(
        self,
        mirrors: Optional[MirrorServiceRepository] = None,
        kits: Optional[KitRepository] = None,
        kit_roles: Optional[KitRoleRepository] = None,
    ) -> None:
        self._mirrors = mirrors or MirrorServiceRepository()
        self._kits = kits or KitRepository()
        self._kit_roles = kit_roles or KitRoleRepository()

    async def apply_observations(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        classification: ClassificationResult,
        observed_at: datetime,
        source: str,
        sync_run_id: Optional[int] = None,
        result_id: Optional[int] = None,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> list[ObservationResult]:
        results: list[ObservationResult] = []
        for obs in classification.observations:
            if obs.tier_id is None:
                continue
            results.append(
                await self._mirrors.apply_observation(
                    session,
                    player_id=player_id,
                    kit_id=obs.kit_id,
                    tier_id=obs.tier_id,
                    discord_role_id=obs.discord_role_id,
                    observed_at=observed_at,
                    source=source,
                    sync_run_id=sync_run_id,
                    result_id=result_id,
                    actor_id=actor_id,
                    actor_name=actor_name,
                    reason=reason,
                )
            )
        return results
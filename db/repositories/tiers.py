"""Current-tier mirror + append-only tier history repositories.

The mirror (``player_current_tiers``) records what Discord has *confirmed*;
it can never be written from a non-Discord authority. Observe/apply semantics
(design §5/§6):

* first observation of a (player, kit) -> insert mirror row + history row with
  ``previous_tier_id = NULL`` (an origin, not a transition)
* same tier re-observed -> refresh timestamps/context only, NO history row
* different tier observed -> update mirror + history row carrying the previous
  tier id

There is deliberately NO unobserve/clear capability here: a member missing the
tier role is an anomaly reported by the sync layer, not a mirror deletion
(clearing is a later-phase, explicitly-reviewed operation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PlayerCurrentTier, TierHistory


@dataclass(frozen=True)
class ObservationResult:
    tier_changed: bool
    first_observation: bool
    previous_tier_id: Optional[int]
    current_tier_id: int
    history_id: Optional[int]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MirrorRepository:
    """Reads/observations of ``player_current_tiers`` (Discord mirror only)."""

    async def get_current(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        kit_id: int,
    ) -> Optional[PlayerCurrentTier]:
        result = await session.execute(
            select(PlayerCurrentTier).where(
                PlayerCurrentTier.player_id == player_id,
                PlayerCurrentTier.kit_id == kit_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_current(
        self,
        session: AsyncSession,
        *,
        player_id: Optional[int] = None,
        kit_id: Optional[int] = None,
    ) -> list[PlayerCurrentTier]:
        stmt = select(PlayerCurrentTier)
        if player_id is not None:
            stmt = stmt.where(PlayerCurrentTier.player_id == player_id)
        if kit_id is not None:
            stmt = stmt.where(PlayerCurrentTier.kit_id == kit_id)
        result = await session.execute(stmt.order_by(PlayerCurrentTier.player_id, PlayerCurrentTier.kit_id))
        return list(result.scalars())


class TierHistoryRepository:
    """Append-only history. No update/delete methods are exposed by design."""

    async def append(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        kit_id: int,
        tier_id: int,
        changed_at: datetime,
        source: str,
        previous_tier_id: Optional[int] = None,
        result_id: Optional[int] = None,
        sync_run_id: Optional[int] = None,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> TierHistory:
        row = TierHistory(
            player_id=player_id,
            kit_id=kit_id,
            tier_id=tier_id,
            previous_tier_id=previous_tier_id,
            changed_at=changed_at,
            source=source,
            result_id=result_id,
            sync_run_id=sync_run_id,
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
        )
        session.add(row)
        await session.flush()
        return row

    async def list_for_player(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        kit_id: Optional[int] = None,
        limit: int = 100,
    ) -> list[TierHistory]:
        stmt = select(TierHistory).where(TierHistory.player_id == player_id)
        if kit_id is not None:
            stmt = stmt.where(TierHistory.kit_id == kit_id)
        result = await session.execute(
            stmt.order_by(TierHistory.changed_at.desc()).limit(limit)
        )
        return list(result.scalars())


class MirrorServiceRepository:
    """Combined mirror apply: transitions the mirror + appends history in one
    flush. Callers own the surrounding transaction."""

    async def apply_observation(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        kit_id: int,
        tier_id: int,
        observed_at: datetime,
        source: str,
        discord_role_id: Optional[int] = None,
        result_id: Optional[int] = None,
        sync_run_id: Optional[int] = None,
        actor_id: Optional[int] = None,
        actor_name: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> ObservationResult:
        observed_at = observed_at or _utcnow()

        # H4 audit fix: serialize read-decide-write for this (player_id,
        # kit_id) pair across concurrent transactions. Without this, two
        # concurrent observations/promotions for the same player+kit (e.g. an
        # outbox replay racing a live /result, or overlapping /result and
        # /topresult) could both read the same pre-image mirror row and both
        # insert a tier_history row claiming the same stale previous_tier_id
        # — silently corrupting the append-only, documented-authoritative
        # history table. A transaction-scoped Postgres advisory lock (held
        # only for this transaction's lifetime, released automatically on
        # commit/rollback) also correctly covers the "no row exists yet"
        # first-observation case, where a plain `SELECT ... FOR UPDATE`
        # cannot lock a row that doesn't exist — two concurrent
        # first-observations now block on each other instead of racing to
        # INSERT and relying on the unique constraint to reject the loser.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:player_id, :kit_id)"),
            {"player_id": int(player_id), "kit_id": int(kit_id)},
        )

        current = await MirrorRepository().get_current(
            session, player_id=player_id, kit_id=kit_id
        )
        if current is None:
            mirror = PlayerCurrentTier(
                player_id=player_id,
                kit_id=kit_id,
                tier_id=tier_id,
                discord_role_id=discord_role_id,
                observed_at=observed_at,
                source=source,
                sync_run_id=sync_run_id,
                result_id=result_id,
            )
            session.add(mirror)
            await session.flush()
            history = await TierHistoryRepository().append(
                session,
                player_id=player_id,
                kit_id=kit_id,
                tier_id=tier_id,
                changed_at=observed_at,
                source=source,
                result_id=result_id,
                sync_run_id=sync_run_id,
                actor_id=actor_id,
                actor_name=actor_name,
                reason=reason,
            )
            return ObservationResult(
                tier_changed=True,
                first_observation=True,
                previous_tier_id=None,
                current_tier_id=tier_id,
                history_id=history.id,
            )

        if current.tier_id == tier_id:
            current.observed_at = observed_at
            current.discord_role_id = discord_role_id
            current.source = source
            current.sync_run_id = sync_run_id
            current.result_id = result_id
            await session.flush()
            return ObservationResult(
                tier_changed=False,
                first_observation=False,
                previous_tier_id=tier_id,
                current_tier_id=tier_id,
                history_id=None,
            )

        previous = current.tier_id
        current.tier_id = tier_id
        current.discord_role_id = discord_role_id
        current.observed_at = observed_at
        current.source = source
        current.sync_run_id = sync_run_id
        current.result_id = result_id
        await session.flush()
        history = await TierHistoryRepository().append(
            session,
            player_id=player_id,
            kit_id=kit_id,
            tier_id=tier_id,
            previous_tier_id=previous,
            changed_at=observed_at,
            source=source,
            result_id=result_id,
            sync_run_id=sync_run_id,
            actor_id=actor_id,
            actor_name=actor_name,
            reason=reason,
        )
        return ObservationResult(
            tier_changed=True,
            first_observation=False,
            previous_tier_id=previous,
            current_tier_id=tier_id,
            history_id=history.id,
        )
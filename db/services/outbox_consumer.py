"""Outbox consumer — replays wedged Discord-confirmed events into PostgreSQL.

When Discord applied a promotion role change but the PostgreSQL write failed,
``/result``/``/topresult`` wedge a ``promotion_commit`` event with
``discord_role_confirmed=True``. This consumer replays those events so the DB
converges to what Discord ALREADY did — it NEVER touches Discord, and it refuses
(dead-letters loudly) any event lacking the confirmation flag. Replays are
idempotent per ``result_key``; failures retry up to ``OUTBOX_MAX_ATTEMPTS``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.repositories.outbox import (
    OUTBOX_DEAD_LETTER,
    OutboxRepository,
)
from db.repositories.sync_audit import AuditRepository
from db.services.promotion import (
    WEDGE_EVENT_TYPE,
    CooldownSpec,
    PromotionCommitService,
    resolve_promotion_dimensions,
)
from db.services.session import transaction

log = logging.getLogger("dachshundtiers.db.outbox")

DEFAULT_STALE_LOCK_SECONDS = 5 * 60
DEFAULT_MAX_EVENTS_PER_PASS = 100
PAYLOAD_VERSION = 1

CONSUMED_DONE = "done"
CONSUMED_ALREADY_COMMITTED = "already_committed"
CONSUMED_REFUSED = "refused"
CONSUMED_RETRY = "retry"
CONSUMED_DEAD_LETTER = "dead_letter"

#: 1:1 passthrough to commit_after_discord_success kwargs.
_SCALAR_FIELDS = (
    "previous_tier_id",
    "bridge_tier_id",
    "tier_status",
    "score",
    "outcome",
    "evaluator_id",
    "ticket_channel_id",
    "opponent_id",
    "opponent_name",
    "notes",
    "eval_flag",
    "date",
    "subtype",
    "close_ticket_channel_id",
    "audit_actor_id",
    "audit_actor_name",
)

_REQUIRED_FIELDS = (
    "result_key",
    "kind",
    "player_id",
    "kit_id",
    "new_tier_id",
    "discord_role_id",
)


@dataclass(frozen=True)
class OutboxConsumption:
    """Result of processing one outbox event."""

    event_id: int
    event_type: str
    aggregate_id: str
    outcome: str
    attempts: int
    error: Optional[str] = None


def _parse_dt(value, field_name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return parsed
    raise ValueError(f"{field_name} must be an ISO-8601 string or datetime")


def _parse_cooldowns(cooldowns_raw) -> tuple[CooldownSpec, ...]:
    specs = []
    for raw in cooldowns_raw or ():
        if not isinstance(raw, dict):
            raise ValueError("cooldowns entries must be objects")
        cooldown_type = raw.get("cooldown_type")
        expires_at = raw.get("expires_at")
        if cooldown_type is None or expires_at is None:
            raise ValueError("cooldown entries need 'cooldown_type' and 'expires_at'")
        specs.append(
            CooldownSpec(
                cooldown_type=str(cooldown_type),
                expires_at=_parse_dt(expires_at, "cooldowns[].expires_at"),
                kit_id=raw.get("kit_id"),
            )
        )
    return tuple(specs)


def default_stale_cutoff(*, seconds: int = DEFAULT_STALE_LOCK_SECONDS) -> datetime:
    """Cutoff for reclaiming ``in_progress`` events orphaned by a crash."""
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def resolve_unresolved_payload_kwargs(
    session_factory: async_sessionmaker[AsyncSession], payload: dict
) -> dict:
    """Build ``commit_after_discord_success`` kwargs from an UNRESOLVED wedge
    payload (``resolved: False`` — C2 audit fix): raw discord_id/ign/kit_key/
    tier codes instead of numeric FKs, wedged because identity/kit/tier
    resolution itself failed at the time Discord was mutated. Re-runs the
    SAME resolution logic (``resolve_promotion_dimensions``) at replay time —
    e.g. an admin has since registered the missing kit, or a transient DB
    error has since cleared.

    Raises :class:`ValueError`/:class:`PlayerIdentityError` (never silently)
    when resolution still fails, so the consumer's normal retry/dead-letter
    handling applies exactly as it would for any other replay failure.
    """
    required = ("result_key", "kind", "discord_id", "ign", "kit_key", "new_tier_code", "discord_role_id")
    missing = [name for name in required if payload.get(name) is None]
    if missing:
        raise ValueError(f"unresolved payload missing required field(s): {', '.join(missing)}")

    async with transaction(session_factory) as session:
        resolved = await resolve_promotion_dimensions(
            session,
            discord_id=payload["discord_id"],
            ign=payload["ign"],
            kit_key=payload["kit_key"],
            new_tier_code=payload["new_tier_code"],
            previous_tier_code=payload.get("previous_tier_code"),
            bridge_tier_code=payload.get("bridge_tier_code"),
            evaluator_discord_id=payload.get("evaluator_discord_id"),
        )
    if resolved.missing:
        raise ValueError(
            f"unresolved payload still missing dimensions: {', '.join(resolved.missing)} "
            f"({payload['kit_key']}/{payload['new_tier_code']})"
        )

    kwargs: dict = {
        "result_key": payload["result_key"],
        "kind": payload["kind"],
        "player_id": resolved.player_id,
        "kit_id": resolved.kit_id,
        "new_tier_id": resolved.new_tier_id,
        "discord_role_id": payload["discord_role_id"],
        "previous_tier_id": resolved.previous_tier_id,
        "bridge_tier_id": resolved.bridge_tier_id,
    }
    # previous_tier_id/bridge_tier_id/evaluator_id are already resolved
    # above from *_code / *_discord_id — the raw payload never carries them
    # directly under these names, so skip re-copying them here.
    already_resolved = {"previous_tier_id", "bridge_tier_id", "evaluator_id"}
    for name in _SCALAR_FIELDS:
        if name in already_resolved:
            continue
        if payload.get(name) is not None:
            kwargs[name] = payload[name]
    if resolved.evaluator_player_id is not None:
        kwargs["evaluator_id"] = resolved.evaluator_player_id

    recorded_at = payload.get("recorded_at")
    if recorded_at is not None:
        kwargs["recorded_at"] = _parse_dt(recorded_at, "recorded_at")

    cooldowns = _parse_cooldowns(payload.get("cooldowns"))
    if cooldowns:
        kwargs["cooldowns"] = cooldowns
    return kwargs


def build_commit_kwargs(payload: dict) -> dict:
    """Normalize a wedged ``promotion_commit`` payload into service kwargs.

    Raises :class:`ValueError` with a precise reason for malformed payloads —
    the consumer turns that into a loud retry/dead-letter, never a silent drop.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")
    if payload.get("version", PAYLOAD_VERSION) != PAYLOAD_VERSION:
        raise ValueError(f"unsupported payload version: {payload.get('version')!r}")

    missing = [name for name in _REQUIRED_FIELDS if payload.get(name) is None]
    if missing:
        raise ValueError(f"payload missing required field(s): {', '.join(missing)}")

    kwargs = {name: payload[name] for name in _REQUIRED_FIELDS}
    for name in _SCALAR_FIELDS:
        if payload.get(name) is not None:
            kwargs[name] = payload[name]

    recorded_at = payload.get("recorded_at")
    if recorded_at is not None:
        kwargs["recorded_at"] = _parse_dt(recorded_at, "recorded_at")

    cooldowns = _parse_cooldowns(payload.get("cooldowns"))
    if cooldowns:
        kwargs["cooldowns"] = cooldowns
    return kwargs


class OutboxConsumer:
    """Leaderless replay loop over the outbox. NEVER mutates Discord."""

    def __init__(
        self,
        outbox: Optional[OutboxRepository] = None,
        promotions: Optional[PromotionCommitService] = None,
        audit: Optional[AuditRepository] = None,
    ) -> None:
        self._outbox = outbox or OutboxRepository()
        self._promotions = promotions or PromotionCommitService()
        self._audit = audit or AuditRepository()

    async def consume_one(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_type: str = WEDGE_EVENT_TYPE,
        in_progress_before: Optional[datetime] = None,
    ) -> Optional[OutboxConsumption]:
        """Claim + replay one event; ``None`` when the outbox is empty."""
        async with transaction(session_factory) as session:
            event = await self._outbox.claim_next(
                session, event_type=event_type, in_progress_before=in_progress_before
            )
            if event is None:
                return None
            event_id = event.id
            event_type_actual = event.event_type
            aggregate_id = event.aggregate_id
            attempts_before = (event.attempts or 1) - 1
            claimed_at = event.claimed_at
            confirmed = event.discord_role_confirmed
            payload = event.payload or {}
        # Claim commits here; a crash mid-replay leaves a stale in_progress the
        # next pass reclaims.
        if confirmed is not True:
            return await self._refuse(
                session_factory,
                event_id=event_id,
                event_type=event_type_actual,
                aggregate_id=aggregate_id,
                attempts_before=attempts_before,
                claimed_at=claimed_at,
            )

        result_key = aggregate_id
        try:
            payload_result_key = payload.get("result_key")
            if (
                payload_result_key is not None
                and str(payload_result_key) != str(result_key)
            ):
                raise ValueError(
                    f"payload result_key {payload_result_key!r} does not match "
                    f"outbox aggregate_id {result_key!r}"
                )
            # C2 audit fix: "resolved: False" (see
            # db/services/promotion._raw_wedge_payload) means Discord
            # mutated but identity/kit/tier resolution never completed the
            # first time — re-resolve now, at replay time, instead of the
            # normal already-resolved-FK payload path.
            if payload.get("resolved", True) is False:
                kwargs = await resolve_unresolved_payload_kwargs(
                    session_factory, payload
                )
            else:
                kwargs = build_commit_kwargs(payload)
            outcome_obj = await self._promotions.commit_after_discord_success(
                session_factory, **kwargs
            )
        except Exception as exc:  # noqa: BLE001 — failures must be loud, never silent
            log.warning(
                "Outbox event %s (%s) replay failed: %s",
                event_id,
                result_key,
                exc,
            )
            return await self._replay_failed(
                session_factory,
                event_id=event_id,
                event_type=event_type_actual,
                aggregate_id=aggregate_id,
                attempts_before=attempts_before,
                error=str(exc),
                result_key=result_key,
                claimed_at=claimed_at,
            )

        async with transaction(session_factory) as session:
            if not await self._outbox.mark_done(
                session, event_id=event_id, claimed_at=claimed_at
            ):
                log.warning(
                    "Outbox event %s (%s) was reclaimed by another consumer "
                    "before completion; replay is idempotent",
                    event_id,
                    result_key,
                )
            await self._audit.append(
                session,
                action="outbox_committed",
                entity_type="outbox_event",
                entity_id=str(event_id),
                details={
                    "result_key": result_key,
                    "already_committed": outcome_obj.already_committed,
                },
            )
        outcome = (
            CONSUMED_ALREADY_COMMITTED
            if outcome_obj.already_committed
            else CONSUMED_DONE
        )
        return OutboxConsumption(
            event_id=event_id,
            event_type=event_type_actual,
            aggregate_id=aggregate_id,
            outcome=outcome,
            attempts=attempts_before + 1,
        )

    async def consume_many(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_events: int = DEFAULT_MAX_EVENTS_PER_PASS,
        event_type: str = WEDGE_EVENT_TYPE,
        in_progress_before: Optional[datetime] = None,
    ) -> list[OutboxConsumption]:
        """Process up to ``max_events`` events; stops early when empty."""
        consumed: list[OutboxConsumption] = []
        for _ in range(max_events):
            item = await self.consume_one(
                session_factory,
                event_type=event_type,
                in_progress_before=in_progress_before,
            )
            if item is None:
                break
            consumed.append(item)
        return consumed

    async def _refuse(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_id: int,
        event_type: str,
        aggregate_id: str,
        attempts_before: int,
        claimed_at: Optional[datetime],
    ) -> OutboxConsumption:
        error = (
            "refusing unconfirmed outbox event: discord_role_confirmed is not True"
        )
        async with transaction(session_factory) as session:
            status = await self._outbox.mark_failed(
                session, event_id=event_id, error=error, claimed_at=claimed_at
            )
            await self._audit.append(
                session,
                action="outbox_refused",
                entity_type="outbox_event",
                entity_id=str(event_id),
                details={"error": error, "aggregate_id": aggregate_id},
            )
        log.error(
            "Outbox event %s (%s) refused (unconfirmed) -> %s",
            event_id,
            aggregate_id,
            status,
        )
        return OutboxConsumption(
            event_id=event_id,
            event_type=event_type,
            aggregate_id=aggregate_id,
            outcome=CONSUMED_DEAD_LETTER
            if status == OUTBOX_DEAD_LETTER
            else CONSUMED_REFUSED,
            attempts=attempts_before + 1,
            error=error,
        )

    async def _replay_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_id: int,
        event_type: str,
        aggregate_id: str,
        attempts_before: int,
        error: str,
        result_key: str,
        claimed_at: Optional[datetime],
    ) -> OutboxConsumption:
        async with transaction(session_factory) as session:
            status = await self._outbox.mark_failed(
                session, event_id=event_id, error=error, claimed_at=claimed_at
            )
            await self._audit.append(
                session,
                action="outbox_failed",
                entity_type="outbox_event",
                entity_id=str(event_id),
                details={"error": error[:2000], "result_key": result_key},
            )
        if status == OUTBOX_DEAD_LETTER:
            log.error(
                "Outbox event %s (%s) dead-lettered after %d attempts — manual "
                "review required",
                event_id,
                result_key,
                attempts_before + 1,
            )
            return OutboxConsumption(
                event_id=event_id,
                event_type=event_type,
                aggregate_id=aggregate_id,
                outcome=CONSUMED_DEAD_LETTER,
                attempts=attempts_before + 1,
                error=error,
            )
        return OutboxConsumption(
            event_id=event_id,
            event_type=event_type,
            aggregate_id=aggregate_id,
            outcome=CONSUMED_RETRY,
            attempts=attempts_before + 1,
            error=error,
        )
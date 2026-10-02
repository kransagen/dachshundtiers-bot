"""Promotion commit service — persist Discord-CONFIRMED promotion results.

Order of operations (design §10, invariant 6): the caller has ALREADY changed
Discord roles successfully; this service ONLY mirrors that confirmed outcome
into PostgreSQL. It never triggers Discord mutations.

``commit_after_discord_success`` runs result + mirror + cooldowns + ticket
close + audit in ONE transaction; re-committing the same ``result_key`` is an
idempotent no-op. ``enqueue_promotion_wedge`` runs on a SEPARATE session so it
survives a failed main transaction (invariant 7: reconcile DB FROM Discord).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Result
from db.repositories.cooldowns import CooldownRepository
from db.repositories.kits import KitRepository, TierDefinitionRepository
from db.repositories.outbox import OutboxRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.results import (
    PROMOTION_COMMITTED,
    ResultRepository,
)
from db.repositories.sync_audit import AuditRepository
from db.repositories.tickets import TicketRepository
from db.repositories.tiers import MirrorServiceRepository, ObservationResult
from db.services.session import transaction

log = logging.getLogger("dachshundtiers.db.promotion")

WEDGE_EVENT_TYPE = "promotion_commit"
PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class CooldownSpec:
    cooldown_type: str
    expires_at: datetime
    kit_id: Optional[int] = None


@dataclass(frozen=True)
class PromotionCommitResult:
    result_key: str
    result_id: int
    already_committed: bool
    observation: Optional[ObservationResult] = None


class PromotionCommitService:
    """Single-transaction mirroring of a Discord-confirmed promotion."""

    def __init__(
        self,
        results: Optional[ResultRepository] = None,
        mirrors: Optional[MirrorServiceRepository] = None,
        cooldowns: Optional[CooldownRepository] = None,
        tickets: Optional[TicketRepository] = None,
        audit: Optional[AuditRepository] = None,
    ) -> None:
        self._results = results or ResultRepository()
        self._mirrors = mirrors or MirrorServiceRepository()
        self._cooldowns = cooldowns or CooldownRepository()
        self._tickets = tickets or TicketRepository()
        self._audit = audit or AuditRepository()

    async def commit_after_discord_success(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        result_key: str,
        kind: str,
        player_id: int,
        kit_id: int,
        new_tier_id: int,
        discord_role_id: int,
        recorded_at: Optional[datetime] = None,
        previous_tier_id: Optional[int] = None,
        bridge_tier_id: Optional[int] = None,
        tier_status: Optional[str] = None,
        score: Optional[str] = None,
        outcome: Optional[str] = None,
        evaluator_id: Optional[int] = None,
        ticket_channel_id: Optional[int] = None,
        opponent_id: Optional[int] = None,
        opponent_name: Optional[str] = None,
        notes: Optional[str] = None,
        eval_flag: bool = False,
        date: Optional[str] = None,
        subtype: Optional[str] = None,
        cooldowns: tuple[CooldownSpec, ...] = (),
        close_ticket_channel_id: Optional[int] = None,
        audit_actor_id: Optional[int] = None,
        audit_actor_name: Optional[str] = None,
    ) -> PromotionCommitResult:
        async with transaction(session_factory) as session:
            existing = await self._results.get_by_key(session, result_key)
            if existing is not None and existing.promotion_status == PROMOTION_COMMITTED:
                return PromotionCommitResult(
                    result_key=result_key,
                    result_id=existing.id,
                    already_committed=True,
                )
            if existing is not None:
                existing.kind = kind
                if subtype is not None:
                    existing.subtype = subtype
                existing.player_id = player_id
                existing.kit_id = kit_id
                existing.new_tier_id = new_tier_id
                existing.previous_tier_id = previous_tier_id
                existing.bridge_tier_id = bridge_tier_id
                existing.tier_status = tier_status
                existing.score = score
                existing.outcome = outcome
                if evaluator_id is not None:
                    existing.evaluator_id = evaluator_id
                if ticket_channel_id is not None:
                    existing.ticket_channel_id = ticket_channel_id
                if opponent_id is not None:
                    existing.opponent_id = opponent_id
                if opponent_name is not None:
                    existing.opponent_name = opponent_name
                if notes is not None:
                    existing.notes = notes
                existing.eval_flag = eval_flag
                existing.date = date
                if recorded_at is not None:
                    existing.recorded_at = recorded_at
                existing.promotion_status = PROMOTION_COMMITTED
                result: Result = existing
                await session.flush()
            else:
                result = await self._results.insert(
                    session,
                    result_key=result_key,
                    kind=kind,
                    subtype=subtype,
                    player_id=player_id,
                    evaluator_id=evaluator_id,
                    kit_id=kit_id,
                    ticket_channel_id=ticket_channel_id,
                    previous_tier_id=previous_tier_id,
                    new_tier_id=new_tier_id,
                    bridge_tier_id=bridge_tier_id,
                    tier_status=tier_status,
                    score=score,
                    outcome=outcome,
                    opponent_id=opponent_id,
                    opponent_name=opponent_name,
                    notes=notes,
                    eval_flag=eval_flag,
                    date=date,
                    recorded_at=recorded_at,
                    promotion_status=PROMOTION_COMMITTED,
                )

            observation = await self._mirrors.apply_observation(
                session,
                player_id=player_id,
                kit_id=kit_id,
                tier_id=new_tier_id,
                discord_role_id=discord_role_id,
                observed_at=recorded_at or datetime.now(timezone.utc),
                source="promotion",
                result_id=result.id,
                actor_id=audit_actor_id,
                actor_name=audit_actor_name,
                reason=f"promotion {result_key}",
            )

            for spec in cooldowns:
                # Cooldown identity is (player, kit, type) — a promotion
                # cooldown for kit X must never become a kit-less GLOBAL
                # cooldown that blocks every kit. A spec that omits kit_id
                # falls back to THIS promotion's kit: that is not a guess,
                # it is the kit the cooldown was granted for. (The outbox
                # replay path in `db/services/outbox_consumer.py` passes
                # `raw.get("kit_id")`, so a legacy payload without it lands
                # here too and is normalized the same way.)
                await self._cooldowns.upsert(
                    session,
                    player_id=player_id,
                    cooldown_type=spec.cooldown_type,
                    expires_at=spec.expires_at,
                    kit_id=spec.kit_id if spec.kit_id is not None else kit_id,
                    source="promotion",
                )

            if close_ticket_channel_id is not None:
                await self._tickets.close_by_channel(
                    session, channel_id=close_ticket_channel_id
                )
                tiers = TierDefinitionRepository()
                previous = (
                    await tiers.get_by_id(session, previous_tier_id)
                    if previous_tier_id is not None
                    else None
                )
                new = await tiers.get_by_id(session, new_tier_id)
                await self._audit.append(
                    session,
                    action="result",
                    actor_id=audit_actor_id,
                    actor_name=audit_actor_name or "",
                    entity_type="ticket",
                    entity_id=str(close_ticket_channel_id),
                    details={
                        "details": (
                            f"{previous.code if previous else 'N/A'} → "
                            f"{new.code if new else '?'}"
                        ),
                        "ts": int((recorded_at or datetime.now(timezone.utc)).timestamp() * 1000),
                    },
                )

            await self._audit.append(
                session,
                action="promotion_committed",
                actor_id=audit_actor_id,
                actor_name=audit_actor_name,
                entity_type="result",
                entity_id=str(result.id),
                details={
                    "result_key": result_key,
                    "player_id": player_id,
                    "kit_id": kit_id,
                    "new_tier_id": new_tier_id,
                },
            )
            return PromotionCommitResult(
                result_key=result_key,
                result_id=result.id,
                already_committed=False,
                observation=observation,
            )


@dataclass(frozen=True)
class ResolvedPromotionDimensions:
    """Result of resolving discord_id/ign/kit_key/tier codes into numeric
    FKs. ``missing`` lists which of ``kit``/``tier`` could not be found (a
    config gap, e.g. a kit/tier not yet registered in PostgreSQL) — this is
    NOT the same as an identity conflict (which raises PlayerIdentityError
    instead, since it needs a human decision, not a later retry)."""

    player_id: int
    kit_id: Optional[int]
    new_tier_id: Optional[int]
    previous_tier_id: Optional[int]
    bridge_tier_id: Optional[int]
    evaluator_player_id: Optional[int]
    missing: tuple[str, ...] = ()


async def resolve_promotion_dimensions(
    session: AsyncSession,
    *,
    discord_id: int,
    ign: str,
    kit_key: str,
    new_tier_code: str,
    previous_tier_code: Optional[str] = None,
    bridge_tier_code: Optional[str] = None,
    evaluator_discord_id: Optional[int] = None,
) -> ResolvedPromotionDimensions:
    """Resolve raw identifiers (discord_id/ign/kit_key/tier codes) to the
    numeric FKs ``commit_after_discord_success`` needs.

    Extracted out of ``commit_promotion_with_wedge`` (C2 audit fix) so the
    SAME resolution logic can run either inline (the live /result path) or
    later, at outbox-replay time, against an "unresolved" wedge payload that
    only carried raw identifiers because resolution itself failed the first
    time (see ``commit_promotion_with_wedge`` and
    ``db/services/outbox_consumer.py``).

    Raises :class:`PlayerIdentityError` on a genuine identity conflict
    (needs a human decision — never auto-retried). A missing kit/tier is
    reported via ``missing`` instead of raising, since it's often transient
    from the caller's perspective (e.g. an admin hasn't run ``/addkit`` yet)
    and IS safe to retry later once resolved.
    """
    _, player = await PlayerRepository().claim_discord_id(
        session, discord_id=discord_id, ign=ign
    )
    kit = await KitRepository().get_by_key(session, (kit_key or "").strip().lower())
    tier = await TierDefinitionRepository().get_by_code(session, new_tier_code)
    previous = None
    if previous_tier_code:
        previous = await TierDefinitionRepository().get_by_code(
            session, previous_tier_code
        )
    bridge_tier_id: Optional[int] = None
    if bridge_tier_code:
        bridge = await TierDefinitionRepository().get_by_code(session, bridge_tier_code)
        if bridge is not None:
            bridge_tier_id = bridge.id
    missing = tuple(
        name for name, obj in (("kit", kit), ("tier", tier)) if obj is None
    )
    evaluator_player_id = None
    if evaluator_discord_id is not None:
        evaluator_player = await PlayerRepository().get_by_discord_id(
            session, evaluator_discord_id
        )
        if evaluator_player is not None:
            evaluator_player_id = evaluator_player.id
    return ResolvedPromotionDimensions(
        player_id=player.id,
        kit_id=kit.id if kit is not None else None,
        new_tier_id=tier.id if tier is not None else None,
        previous_tier_id=previous.id if previous is not None else None,
        bridge_tier_id=bridge_tier_id,
        evaluator_player_id=evaluator_player_id,
        missing=missing,
    )


def _raw_wedge_payload(
    *,
    result_key: str,
    kind: str,
    discord_id: int,
    ign: str,
    kit_key: str,
    new_tier_code: str,
    discord_role_id: int,
    previous_tier_code: Optional[str],
    bridge_tier_code: Optional[str],
    tier_status: Optional[str],
    score: Optional[str],
    outcome: Optional[str],
    evaluator_discord_id: Optional[int],
    ticket_channel_id: Optional[int],
    opponent_id: Optional[int],
    opponent_name: Optional[str],
    notes: Optional[str],
    eval_flag: bool,
    date: Optional[str],
    subtype: Optional[str],
    close_ticket_channel_id: Optional[int],
    audit_actor_id: Optional[int],
    audit_actor_name: Optional[str],
    cooldowns: tuple[CooldownSpec, ...],
    recorded_at: Optional[datetime],
) -> dict:
    """Wedge payload for a promotion whose identity/kit/tier resolution has
    NOT yet succeeded (C2 audit fix) — carries raw identifiers instead of
    numeric FKs, so the outbox consumer can retry resolution itself at
    replay time (``resolved: False`` is the marker; see
    ``db/services/outbox_consumer.py``).
    """
    return {
        "version": PAYLOAD_VERSION,
        "resolved": False,
        "result_key": result_key,
        "kind": kind,
        "discord_id": discord_id,
        "ign": ign,
        "kit_key": kit_key,
        "new_tier_code": new_tier_code,
        "discord_role_id": discord_role_id,
        "previous_tier_code": previous_tier_code,
        "bridge_tier_code": bridge_tier_code,
        "tier_status": tier_status,
        "score": score,
        "outcome": outcome,
        "evaluator_discord_id": evaluator_discord_id,
        "ticket_channel_id": ticket_channel_id,
        "opponent_id": opponent_id,
        "opponent_name": opponent_name,
        "notes": notes,
        "eval_flag": eval_flag,
        "date": date,
        "subtype": subtype,
        "close_ticket_channel_id": close_ticket_channel_id,
        "audit_actor_id": audit_actor_id,
        "audit_actor_name": audit_actor_name,
        "recorded_at": (recorded_at or datetime.now(timezone.utc)).isoformat(),
        "cooldowns": [
            {
                "cooldown_type": spec.cooldown_type,
                "expires_at": spec.expires_at.isoformat(),
                "kit_id": spec.kit_id,
            }
            for spec in cooldowns
        ],
    }


async def enqueue_promotion_wedge(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    result_key: str,
    payload: dict,
    discord_role_confirmed: bool = True,
) -> None:
    """Fresh-session enqueue: usable right after the main transaction failed."""
    async with transaction(session_factory) as session:
        await OutboxRepository().enqueue(
            session,
            event_type=WEDGE_EVENT_TYPE,
            aggregate_type="result",
            aggregate_id=result_key,
            payload=payload,
            discord_role_confirmed=discord_role_confirmed,
        )


async def _wedge_unresolved_or_report(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    result_key: str,
    raw_payload: dict,
    reason: str,
) -> "PromotionWedgeOutcome":
    """Try to durably wedge an UNRESOLVED promotion (C2 audit fix).

    Used when Discord already mutated but identity/kit/tier resolution
    itself failed or couldn't complete — a distinct payload shape
    (``resolved: False``, raw identifiers) that the outbox consumer
    re-resolves at replay time (see ``db/services/outbox_consumer.py``).

    If PostgreSQL is unreachable even for the wedge insert itself, this
    NEVER claims the wedge succeeded — it reports the DB as unavailable and
    tells the operator reconciliation/manual `/sync discord` is the only
    remaining recovery path (invariant: never pretend a wedge was persisted
    when it wasn't).
    """
    log.warning(
        "Promotion %s se nepodařilo vyřešit (%s) – wedge do outboxu (unresolved)",
        result_key,
        reason,
    )
    try:
        await enqueue_promotion_wedge(
            session_factory,
            result_key=result_key,
            payload=raw_payload,
            discord_role_confirmed=True,
        )
    except Exception:  # noqa: BLE001 – ani wedge nesmí tiše zmizet
        log.exception("Wedge (unresolved) %s se nepodařilo zařadit", result_key)
        return PromotionWedgeOutcome(
            committed=False,
            wedged=False,
            message=(
                f"⚠️ Discord role se změnila, ale PostgreSQL zápis selhal ({reason}) "
                "A záznam do outboxu také (DB je pravděpodobně nedostupná) – "
                "mirror se musí doplnit ručně (nebo spuštěním /sync discord, "
                "jakmile bude DB dostupná)."
            ),
        )
    return PromotionWedgeOutcome(
        committed=False,
        wedged=True,
        message=(
            f"⚠️ Discord role se změnila, ale PostgreSQL zápis selhal ({reason}) – "
            "událost je v outboxu a mirror se doplní automaticky, jakmile "
            "bude možné identitu/kit/tier vyřešit."
        ),
    )


@dataclass(frozen=True)
class PromotionWedgeOutcome:
    """Výsledek cog-sideho zápisu: commit úspěšný / wedge / nelze zapisovat."""

    committed: bool
    wedged: bool
    message: str = ""


def _wedge_payload(
    *,
    result_key: str,
    kind: str,
    player_id: int,
    kit_id: int,
    new_tier_id: int,
    discord_role_id: int,
    previous_tier_id: Optional[int],
    bridge_tier_id: Optional[int],
    tier_status: Optional[str],
    score: Optional[str],
    outcome: Optional[str],
    evaluator_id: Optional[int],
    ticket_channel_id: Optional[int],
    opponent_id: Optional[int],
    opponent_name: Optional[str],
    notes: Optional[str],
    eval_flag: bool,
    date: Optional[str],
    subtype: Optional[str],
    close_ticket_channel_id: Optional[int],
    audit_actor_id: Optional[int],
    audit_actor_name: Optional[str],
    cooldowns: tuple[CooldownSpec, ...],
    recorded_at: Optional[datetime],
) -> dict:
    """Payload v contractu outbox consumera (``build_commit_kwargs``)."""
    return {
        "version": PAYLOAD_VERSION,
        "result_key": result_key,
        "kind": kind,
        "player_id": player_id,
        "kit_id": kit_id,
        "new_tier_id": new_tier_id,
        "discord_role_id": discord_role_id,
        "previous_tier_id": previous_tier_id,
        "bridge_tier_id": bridge_tier_id,
        "tier_status": tier_status,
        "score": score,
        "outcome": outcome,
        "evaluator_id": evaluator_id,
        "ticket_channel_id": ticket_channel_id,
        "opponent_id": opponent_id,
        "opponent_name": opponent_name,
        "notes": notes,
        "eval_flag": eval_flag,
        "date": date,
        "subtype": subtype,
        "close_ticket_channel_id": close_ticket_channel_id,
        "audit_actor_id": audit_actor_id,
        "audit_actor_name": audit_actor_name,
        "recorded_at": (recorded_at or datetime.now(timezone.utc)).isoformat(),
        "cooldowns": [
            {
                "cooldown_type": spec.cooldown_type,
                "expires_at": spec.expires_at.isoformat(),
                "kit_id": spec.kit_id,
            }
            for spec in cooldowns
        ],
    }


async def commit_promotion_with_wedge(
    session_factory: Optional[async_sessionmaker[AsyncSession]],
    *,
    result_key: str,
    kind: str,
    discord_id: int,
    ign: str,
    kit_key: str,
    new_tier_code: str,
    discord_role_id: int,
    previous_tier_code: Optional[str] = None,
    recorded_at: Optional[datetime] = None,
    bridge_tier_code: Optional[str] = None,
    tier_status: Optional[str] = None,
    score: Optional[str] = None,
    outcome: Optional[str] = None,
    evaluator_discord_id: Optional[int] = None,
    ticket_channel_id: Optional[int] = None,
    opponent_id: Optional[int] = None,
    opponent_name: Optional[str] = None,
    notes: Optional[str] = None,
    eval_flag: bool = False,
    date: Optional[str] = None,
    subtype: Optional[str] = None,
    cooldowns: tuple[CooldownSpec, ...] = (),
    close_ticket_channel_id: Optional[int] = None,
    audit_actor_id: Optional[int] = None,
    audit_actor_name: Optional[str] = None,
) -> PromotionWedgeOutcome:
    """Zapíše Discord-potvrzené povýšení do PostgreSQL; při selhání wedge.

    Volá se PO úspěšné Discord mutaci (invariant 6). Hráč se najde/přijme
    podle ``discord_id`` (stabilní identita), kit/tier se vyřeší z klíče/kódu.
    Selže-li hlavní transakce, událost se zařadí do outboxu
    (``discord_role_confirmed=True`` – disk republikuje DB PODLE Discordu,
    nikdy naopak). Bez session factory nebo bez dimenzí se NEpíše mirror a
    ani wedge – vrátí se hlasitá zpráva.
    """
    if session_factory is None:
        return PromotionWedgeOutcome(
            committed=False,
            wedged=False,
            message=(
                "⚠️ PostgreSQL není nakonfigurováno – role se změnila, "
                "mirror se NEzapsal (a bez DB nelze ani outbox)."
            ),
        )

    # C2 audit fix: EVERY failure mode below — identity conflict, a
    # transient/concurrent DB error during resolution, or a missing
    # kit/tier dimension — now wedges, not just a failure in the second
    # (commit_after_discord_success) transaction. Discord already mutated
    # by the time this function is called; PostgreSQL must always end up
    # with SOME durable trace of that, even when it can't be fully resolved
    # yet, so reconciliation/manual review can complete it later instead of
    # the event silently vanishing after only a log line.
    raw_payload = _raw_wedge_payload(
        result_key=result_key,
        kind=kind,
        discord_id=discord_id,
        ign=ign,
        kit_key=kit_key,
        new_tier_code=new_tier_code,
        discord_role_id=discord_role_id,
        previous_tier_code=previous_tier_code,
        bridge_tier_code=bridge_tier_code,
        tier_status=tier_status,
        score=score,
        outcome=outcome,
        evaluator_discord_id=evaluator_discord_id,
        ticket_channel_id=ticket_channel_id,
        opponent_id=opponent_id,
        opponent_name=opponent_name,
        notes=notes,
        eval_flag=eval_flag,
        date=date,
        subtype=subtype,
        close_ticket_channel_id=close_ticket_channel_id,
        audit_actor_id=audit_actor_id,
        audit_actor_name=audit_actor_name,
        cooldowns=cooldowns,
        recorded_at=recorded_at,
    )

    try:
        async with transaction(session_factory) as session:
            resolved = await resolve_promotion_dimensions(
                session,
                discord_id=discord_id,
                ign=ign,
                kit_key=kit_key,
                new_tier_code=new_tier_code,
                previous_tier_code=previous_tier_code,
                bridge_tier_code=bridge_tier_code,
                evaluator_discord_id=evaluator_discord_id,
            )
    except PlayerIdentityError as err:
        log.warning("Identita %s pro promotion %s odmítnuta: %s", ign, result_key, err)
        return await _wedge_unresolved_or_report(
            session_factory,
            result_key=result_key,
            raw_payload=raw_payload,
            reason=f"identita: {err}",
        )
    except Exception:  # noqa: BLE001 – resolving se nesmí tiše ztratit
        log.exception(
            "Resolving identity/kit/tier pro promotion %s selhal", result_key
        )
        return await _wedge_unresolved_or_report(
            session_factory,
            result_key=result_key,
            raw_payload=raw_payload,
            reason="neočekávaná chyba při resolvingu identity/kit/tieru",
        )

    if resolved.missing:
        return await _wedge_unresolved_or_report(
            session_factory,
            result_key=result_key,
            raw_payload=raw_payload,
            reason=(
                f"chybí dimenze ({', '.join(resolved.missing)}: "
                f"{kit_key}/{new_tier_code})"
            ),
        )

    player_id = resolved.player_id
    kit_id = resolved.kit_id
    new_tier_id = resolved.new_tier_id
    previous_tier_id = resolved.previous_tier_id
    bridge_tier_id = resolved.bridge_tier_id
    evaluator_player_id = resolved.evaluator_player_id

    try:
        await PromotionCommitService().commit_after_discord_success(
            session_factory,
            result_key=result_key,
            kind=kind,
            player_id=player_id,
            kit_id=kit_id,
            new_tier_id=new_tier_id,
            discord_role_id=discord_role_id,
            recorded_at=recorded_at,
            previous_tier_id=previous_tier_id,
            bridge_tier_id=bridge_tier_id,
            tier_status=tier_status,
            score=score,
            outcome=outcome,
            evaluator_id=evaluator_player_id,
            ticket_channel_id=ticket_channel_id,
            opponent_id=opponent_id,
            opponent_name=opponent_name,
            notes=notes,
            eval_flag=eval_flag,
            date=date,
            subtype=subtype,
            cooldowns=cooldowns,
            close_ticket_channel_id=close_ticket_channel_id,
            audit_actor_id=audit_actor_id,
            audit_actor_name=audit_actor_name,
        )
    except Exception:  # noqa: BLE001 – selhání DB se nikdy neschovává
        log.exception("Commit promotion %s selhal – wedge do outboxu", result_key)
        payload = _wedge_payload(
            result_key=result_key,
            kind=kind,
            player_id=player_id,
            kit_id=kit_id,
            new_tier_id=new_tier_id,
            discord_role_id=discord_role_id,
            previous_tier_id=previous_tier_id,
            bridge_tier_id=bridge_tier_id,
            tier_status=tier_status,
            score=score,
            outcome=outcome,
            evaluator_id=evaluator_player_id,
            ticket_channel_id=ticket_channel_id,
            opponent_id=opponent_id,
            opponent_name=opponent_name,
            notes=notes,
            eval_flag=eval_flag,
            date=date,
            subtype=subtype,
            close_ticket_channel_id=close_ticket_channel_id,
            audit_actor_id=audit_actor_id,
            audit_actor_name=audit_actor_name,
            cooldowns=cooldowns,
            recorded_at=recorded_at,
        )
        try:
            await enqueue_promotion_wedge(
                session_factory,
                result_key=result_key,
                payload=payload,
                discord_role_confirmed=True,
            )
        except Exception:  # noqa: BLE001 – ani wedge nesmí tiše zmizet
            log.exception("Wedge %s se nepodařilo zařadit", result_key)
            return PromotionWedgeOutcome(
                committed=False,
                wedged=False,
                message=(
                    "⚠️ Discord role se změnila, ale zápis do PostgreSQL selhal "
                    "A záznam do outboxu také – mirror se musí doplnit ručně "
                    "(nebo spuštěním /sync discord)."
                ),
            )
        return PromotionWedgeOutcome(
            committed=False,
            wedged=True,
            message=(
                "⚠️ Discord role se změnila, ale zápis do PostgreSQL selhal – "
                "událost je v outboxu a mirror se doplní automaticky."
            ),
        )
    return PromotionWedgeOutcome(committed=True, wedged=False, message="")


# ---------------------------------------------------------------------------
# CANONICAL PRODUCTION ENTRY POINT (Phase G0 cutover)
# ---------------------------------------------------------------------------


def grant_confirmation(grant) -> tuple[bool, Optional[int], bool]:
    """Normalise a Discord role-mutation result into
    ``(confirmed, role_id, ambiguous)``.

    ``grant`` is duck-typed on purpose: ``cogs.roles.TierRoleGrant`` lives in
    the Discord layer and must NOT be imported here (``db/`` stays free of any
    Discord dependency). Every attribute is read defensively, so a caller that
    passes ``None``, a wrong object, or a hand-rolled object can never satisfy
    the confirmation requirement.

    A grant is CONFIRMED only when ALL of the following hold:

    * ``ok``        – the mutation was not rejected,
    * ``verified``  – the ACTUAL final Discord role set was read back and
                      matched the intended one (G0/invariant 6); ``ok`` alone
                      only means "the HTTP call did not raise",
    * ``ambiguous`` is falsy – an ambiguous result means the Discord outcome is
                      UNKNOWN, so it can never be treated as a state Discord
                      confirmed, whatever the other flags claim,
    * ``tier_role_id`` is set – there is a concrete role to mirror.
    """
    role_id = getattr(grant, "tier_role_id", None)
    ambiguous = bool(getattr(grant, "ambiguous", False))
    confirmed = (
        bool(getattr(grant, "ok", False))
        and bool(getattr(grant, "verified", False))
        and not ambiguous
        and role_id is not None
    )
    return confirmed, role_id, ambiguous


async def commit_confirmed_promotion(
    session_factory: Optional[async_sessionmaker[AsyncSession]],
    *,
    grant,
    **commit_kwargs,
) -> PromotionWedgeOutcome:
    """The ONE production entry point that persists a promotion.

    Contract (this is the architectural enforcement point, not a convenience
    wrapper — a caller cannot bypass it and still reach the DB):

    1. ``grant`` MUST be a Discord-confirmed role mutation. Anything that is
       not confirmed (rejected, ambiguous, unverified, ``None``, or a wrong
       object) is REFUSED: nothing is written to ``player_current_tiers``,
       ``tier_history`` or ``results``, and a loud operator-facing message is
       returned. That covers failure-matrix cases 1, 2 and 6.
    2. With a confirmed grant, resolve identity/kit/tier and commit result +
       mirror + history + audit in ONE transaction, wedging durably if that
       transaction fails (cases 3, 4, 7).
    3. Without a ``session_factory`` nothing is written and nothing is
       wedged (the legacy no-PostgreSQL deployment, where the Discord role
       stays the only authority and ``/sync discord`` is the repair path).

    This function holds no Discord handle and imports no JSON store, so the
    forbidden directions ``PostgreSQL -> Discord`` and
    ``players.json -> current tier`` are structurally impossible here.
    """
    confirmed, role_id, ambiguous = grant_confirmation(grant)

    if not confirmed:
        if ambiguous:
            reason = (
                "stav rolí v Discordu se nepodařilo potvrdit – je NEJISTÝ, "
                "takže se do PostgreSQL nic nezapsalo (žádný vrat Discordu, "
                "žádný falešný mirror)"
            )
        else:
            reason = (
                "role se v Discordu nepotvrdily jako změněné – do PostgreSQL "
                "se proto nezapsal žádný tier"
            )
        log.warning(
            "Promotion %s z refused (confirmed=%s, ambiguous=%s, role_id=%s): %s",
            commit_kwargs.get("result_key"),
            confirmed,
            ambiguous,
            role_id,
            reason,
        )
        return PromotionWedgeOutcome(
            committed=False,
            wedged=False,
            message=(
                f"⚠️ {reason}. Zkontroluj stav rolí ručně nebo spusť "
                "`/sync discord` (Discord zůstává jediným zdrojem pravdy)."
            ),
        )

    if commit_kwargs.get("discord_role_id") is None:
        commit_kwargs["discord_role_id"] = int(role_id)
    return await commit_promotion_with_wedge(session_factory, **commit_kwargs)
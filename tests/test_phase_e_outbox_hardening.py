"""Phase E, E5 — outbox crash recovery and duplicate delivery hardening.

End-to-end scenarios on real embedded PostgreSQL:

1. Crash after claim: a consumer claims the wedge (in_progress) and the
   process dies before mark_done. On restart, a fresh consumer reclaims the
   stale event via ``in_progress_before`` and the mirror catches up.
   The Discord side stays untouched (the event is ``discord_role_confirmed``
   and is replayed as a DB-only commit).
2. Duplicate delivery: the same promotion lands in the outbox twice; the
   second delivery is idempotent (``ALREADY_COMMITTED``), zero extra
   history rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import OutboxEvent, Result, TierHistory
from db.repositories.outbox import OUTBOX_DONE, OUTBOX_IN_PROGRESS, OutboxRepository
from db.repositories.tiers import MirrorRepository
from db.services.outbox_consumer import (
    CONSUMED_ALREADY_COMMITTED,
    CONSUMED_DONE,
    OutboxConsumer,
    default_stale_cutoff,
)
from db.services.promotion import enqueue_promotion_wedge
from db.services.reconciliation import ReconciliationService
from db.services.session import transaction

from tests.test_services_outbox_consumer import _seed, _valid_payload


async def _wedge(session_factory, seeded, *, key="e5-crash"):
    await enqueue_promotion_wedge(
        session_factory,
        result_key=key,
        payload=_valid_payload(seeded, result_key=key),
        discord_role_confirmed=True,
    )


async def test_crash_after_claim_recovers_on_restart(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await _wedge(session_factory, seeded, key="e5-crash")

    async with transaction(session_factory) as session:
        row = (
            await session.execute(select(OutboxEvent))
        ).scalar_one()
        row.status = OUTBOX_IN_PROGRESS
        # M7 audit fix: staleness is measured from claimed_at (when the row
        # was actually claimed), not created_at (when it was enqueued) — a
        # merely-backlogged-then-claimed event must NOT look stale the
        # instant it's claimed. Simulate a crash: claimed 2h ago, never
        # finished.
        row.claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.flush()

    cutoff = default_stale_cutoff()
    consumption = await OutboxConsumer().consume_one(
        session_factory, in_progress_before=cutoff
    )
    assert consumption is not None and consumption.outcome == CONSUMED_DONE

    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "e5-crash")
            )
        ).scalar_one()
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        events = await OutboxRepository().list_by_status(session, status=OUTBOX_DONE)
    assert result.promotion_status == "committed"
    assert mirror is not None and mirror.source == "promotion"
    assert len(events) == 1 and events[0].discord_role_confirmed is True


async def test_reconciliation_service_reclaims_stale_claim_through_real_entry_point(
    session_factory, clean_db
):
    """C1 audit fix, integration-level: exercises the ACTUAL production
    entry point (bot.py calls ReconciliationService.reconcile() at startup
    and hourly), not OutboxConsumer.consume_one() directly. Before the fix,
    reconcile() called consume_many() with no in_progress_before at all, so
    a stale in_progress event was invisible to every future reconciliation
    pass, forever — this test would have hung at 'nothing reclaimed' on the
    old code (see test_crash_after_claim_recovers_on_restart for the
    lower-level equivalent that masked this gap by calling the consumer
    directly)."""
    seeded = await _seed(session_factory)
    await _wedge(session_factory, seeded, key="e5-reconcile-crash")

    async with transaction(session_factory) as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
        row.status = OUTBOX_IN_PROGRESS
        row.claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await session.flush()

    outcome = await ReconciliationService().reconcile(session_factory, members=[])

    assert len(outcome.outbox_consumed) == 1
    assert outcome.outbox_consumed[0].outcome == CONSUMED_DONE

    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "e5-reconcile-crash")
            )
        ).scalar_one()
        events = await OutboxRepository().list_by_status(session, status=OUTBOX_DONE)
    assert result.promotion_status == "committed"
    assert len(events) == 1


async def test_reconciliation_service_does_not_reclaim_active_claim(
    session_factory, clean_db
):
    """A claim that is genuinely fresh (not stale) must NOT be stolen by a
    concurrent/subsequent reconciliation pass — only claims older than the
    stale-lock window are eligible."""
    seeded = await _seed(session_factory)
    await _wedge(session_factory, seeded, key="e5-reconcile-active")

    async with transaction(session_factory) as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
        row.status = OUTBOX_IN_PROGRESS
        row.claimed_at = datetime.now(timezone.utc)  # claimed "just now"
        await session.flush()

    outcome = await ReconciliationService().reconcile(session_factory, members=[])
    assert outcome.outbox_consumed == ()

    async with transaction(session_factory) as session:
        row = (await session.execute(select(OutboxEvent))).scalar_one()
    assert row.status == OUTBOX_IN_PROGRESS, (
        "aktivní (nestárnoucí) claim nesmí být ukraden souběžným reconcile průchodem"
    )


async def test_duplicate_delivery_is_idempotent(session_factory, clean_db):
    seeded = await _seed(session_factory)
    key = "e5-dupe"
    await _wedge(session_factory, seeded, key=key)

    first = await OutboxConsumer().consume_one(session_factory)
    assert first is not None and first.outcome == CONSUMED_DONE

    await _wedge(session_factory, seeded, key=key)
    second = await OutboxConsumer().consume_one(session_factory)
    assert second is not None and second.outcome == CONSUMED_ALREADY_COMMITTED

    async with transaction(session_factory) as session:
        history = (
            await session.execute(
                select(TierHistory).where(
                    TierHistory.player_id == seeded["player"].id
                )
            )
        ).scalars().all()
        events = await OutboxRepository().list_by_status(session, status=OUTBOX_DONE)
    assert len(history) == 1
    assert len(events) == 2


async def test_outbox_never_touches_discord_on_replay(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await _wedge(session_factory, seeded, key="e5-nodiscord")

    consumed = await OutboxConsumer().consume_many(session_factory, max_events=10)
    assert len(consumed) == 1 and consumed[0].outcome == CONSUMED_DONE

    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "e5-nodiscord")
            )
        ).scalar_one()
    assert result.promotion_status == "committed"
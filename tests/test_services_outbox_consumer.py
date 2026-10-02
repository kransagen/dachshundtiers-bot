"""OutboxConsumer tests — replays Discord-confirmed wedges, refuses the rest.

Covers: successful replay, idempotent re-commit, unconfirmed refusal,
dead-letter after max attempts, stale in_progress crash recovery, payload
validation failures, aggregate/result_key mismatch, consume_many batching.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from db.models import AuditLog, OutboxEvent, Result, TierHistory
from db.repositories.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DONE,
    OUTBOX_PENDING,
    OutboxRepository,
)
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorRepository
from db.services.outbox_consumer import (
    CONSUMED_ALREADY_COMMITTED,
    CONSUMED_DEAD_LETTER,
    CONSUMED_DONE,
    CONSUMED_REFUSED,
    CONSUMED_RETRY,
    OutboxConsumer,
    build_commit_kwargs,
    default_stale_cutoff,
)
from db.services.promotion import (
    PromotionCommitService,
    enqueue_promotion_wedge,
)
from db.services.session import transaction


async def _skip_backoff(session_factory):
    async with transaction(session_factory) as session:
        await session.execute(update(OutboxEvent).values(next_attempt_at=None))


async def _seed(session_factory):
    from db.models import Kit, TierDefinition
    from db.repositories.kits import ensure_dimensions

    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t2 = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "t2")
        )).scalar_one()
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Promo"
        )
        _, evaluator = await PlayerRepository().claim_discord_id(
            session, discord_id=2222, ign="TesterPromo"
        )
        return {"kit": kit, "player": player, "tier": t2, "evaluator": evaluator}


def _valid_payload(seeded, *, result_key="promo-consume", **overrides):
    now = datetime.now(timezone.utc)
    payload = {
        "version": 1,
        "result_key": result_key,
        "kind": "ticket",
        "player_id": seeded["player"].id,
        "kit_id": seeded["kit"].id,
        "new_tier_id": seeded["tier"].id,
        "discord_role_id": 777777,
        "previous_tier_id": None,
        "evaluator_id": seeded["evaluator"].id,
        "notes": "from wedge",
        "eval_flag": False,
        "recorded_at": now.isoformat(),
        "date": "2026-09-25",
        "cooldowns": [
            {
                "cooldown_type": "ht3",
                "expires_at": (now + timedelta(days=7)).isoformat(),
                "kit_id": seeded["kit"].id,
            }
        ],
        "audit_actor_id": 42,
        "audit_actor_name": "Mod",
    }
    payload.update(overrides)
    return payload


async def test_consume_commits_wedged_event(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-consume",
        payload=_valid_payload(seeded),
        discord_role_confirmed=True,
    )

    consumption = await OutboxConsumer().consume_one(session_factory)
    assert consumption is not None
    assert consumption.outcome == CONSUMED_DONE

    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "promo-consume")
            )
        ).scalar_one()
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        history = (
            await session.execute(select(TierHistory))
        ).scalars().all()
        audits = (
            await session.execute(select(AuditLog))
        ).scalars().all()
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(
            session, status=OUTBOX_DONE
        )
    assert result.promotion_status == "committed"
    assert result.notes == "from wedge"
    assert result.evaluator_id == seeded["evaluator"].id
    assert mirror is not None and mirror.source == "promotion"
    assert len(history) == 1 and history[0].source == "promotion"
    assert audits and any(a.action == "outbox_committed" for a in audits)
    assert [e.aggregate_id for e in events] == ["promo-consume"]


async def test_consume_idempotent_when_already_committed(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    await PromotionCommitService().commit_after_discord_success(
        session_factory,
        result_key="promo-dup",
        kind="ticket",
        player_id=seeded["player"].id,
        kit_id=seeded["kit"].id,
        new_tier_id=seeded["tier"].id,
        discord_role_id=777778,
    )
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-dup",
        payload=_valid_payload(seeded, result_key="promo-dup"),
        discord_role_confirmed=True,
    )

    consumption = await OutboxConsumer().consume_one(session_factory)
    assert consumption.outcome == CONSUMED_ALREADY_COMMITTED

    async with transaction(session_factory) as session:
        history = (await session.execute(select(TierHistory))).scalars().all()
    assert len(history) == 1


async def test_consume_refuses_unconfirmed_event(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-unconfirmed",
        payload=_valid_payload(seeded, result_key="promo-unconfirmed"),
        discord_role_confirmed=False,
    )

    consumer = OutboxConsumer()
    consumption = await consumer.consume_one(session_factory)
    assert consumption.outcome == CONSUMED_REFUSED
    assert "unconfirmed" in consumption.error

    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "promo-unconfirmed")
            )
        ).scalar_one_or_none()
        audits = (await session.execute(select(AuditLog))).scalars().all()
        events = await OutboxRepository().list_by_status(
            session, status=OUTBOX_PENDING
        )
    assert result is None
    assert any(a.action == "outbox_refused" for a in audits)
    assert len(events) == 1 and events[0].attempts == 1


async def test_consume_unconfirmed_retries_until_dead_letter(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-unconfirmed-2",
        payload=_valid_payload(seeded, result_key="promo-unconfirmed-2"),
        discord_role_confirmed=False,
    )
    consumer = OutboxConsumer()
    outcomes = []
    for _ in range(5):
        await _skip_backoff(session_factory)
        consumption = await consumer.consume_one(session_factory)
        assert consumption is not None
        outcomes.append(consumption.outcome)
    assert outcomes == [CONSUMED_REFUSED] * 4 + [CONSUMED_DEAD_LETTER]
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(
            session, status=OUTBOX_DEAD_LETTER
        )
    assert [e.aggregate_id for e in events] == ["promo-unconfirmed-2"]


async def test_consume_dead_letters_after_max_attempts(session_factory, clean_db):
    await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-broken",
        payload={"result_key": "promo-broken", "kind": "ticket"},
        discord_role_confirmed=True,
    )
    consumer = OutboxConsumer()
    outcomes = []
    for _ in range(5):
        await _skip_backoff(session_factory)
        consumption = await consumer.consume_one(session_factory)
        assert consumption is not None
        outcomes.append(consumption.outcome)
    assert outcomes == [CONSUMED_RETRY] * 4 + [CONSUMED_DEAD_LETTER]
    async with transaction(session_factory) as session:
        dead = await OutboxRepository().list_by_status(
            session, status=OUTBOX_DEAD_LETTER
        )
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "promo-broken")
            )
        ).scalar_one_or_none()
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert len(dead) == 1
    assert dead[0].attempts == 5
    assert "required" in dead[0].last_error
    assert result is None
    assert sum(a.action == "outbox_failed" for a in audits) == 5


async def test_consume_reclaims_stale_in_progress(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-stale",
        payload=_valid_payload(seeded, result_key="promo-stale"),
        discord_role_confirmed=True,
    )
    stale_before = datetime.now(timezone.utc) - timedelta(hours=2)
    async with transaction(session_factory) as session:
        from db.models import OutboxEvent

        row = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == "promo-stale")
            )
        ).scalar_one()
        row.status = "in_progress"
        # M7 audit fix: staleness is measured from claimed_at, not created_at.
        row.claimed_at = stale_before - timedelta(minutes=5)
        await session.flush()

    consumption = await OutboxConsumer().consume_one(
        session_factory, in_progress_before=stale_before
    )
    assert consumption is not None and consumption.outcome == CONSUMED_DONE
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(
            session, status=OUTBOX_DONE
        )
    assert [e.aggregate_id for e in events] == ["promo-stale"]


async def test_consume_result_key_mismatch_fails_loud(session_factory, clean_db):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await OutboxRepository().enqueue(
            session,
            event_type="promotion_commit",
            aggregate_type="result",
            aggregate_id="promo-agg",
            payload=_valid_payload(seeded, result_key="promo-payload"),
            discord_role_confirmed=True,
        )

    consumption = await OutboxConsumer().consume_one(session_factory)
    assert consumption.outcome == CONSUMED_RETRY
    assert "does not match" in consumption.error


async def test_consume_many_processes_all_then_stops(session_factory, clean_db):
    seeded = await _seed(session_factory)
    for idx in range(3):
        key = f"promo-batch-{idx}"
        await enqueue_promotion_wedge(
            session_factory,
            result_key=key,
            payload=_valid_payload(seeded, result_key=key),
            discord_role_confirmed=True,
        )
    consumed = await OutboxConsumer().consume_many(
        session_factory, max_events=10
    )
    assert [c.aggregate_id for c in consumed] == [
        "promo-batch-0",
        "promo-batch-1",
        "promo-batch-2",
    ]
    assert all(c.outcome == CONSUMED_DONE for c in consumed)
    assert await OutboxConsumer().consume_many(session_factory) == []


async def test_consume_empty_outbox_returns_none(session_factory, clean_db):
    consumption = await OutboxConsumer().consume_one(session_factory)
    assert consumption is None


async def test_build_commit_kwargs_converts_and_validates(session_factory, clean_db):
    seeded = await _seed(session_factory)
    now = datetime.now(timezone.utc)
    kwargs = build_commit_kwargs(
        _valid_payload(
            seeded,
            recorded_at=now.isoformat(),
            cooldowns=[
                {
                    "cooldown_type": "ht3",
                    "expires_at": (now + timedelta(hours=1)).isoformat(),
                }
            ],
        )
    )
    assert kwargs["recorded_at"].tzinfo is not None
    assert kwargs["recorded_at"] == now
    assert isinstance(kwargs["cooldowns"], tuple)
    assert kwargs["cooldowns"][0].cooldown_type == "ht3"
    assert kwargs["eval_flag"] is False

    with pytest.raises(ValueError, match="required field"):
        build_commit_kwargs({"version": 1, "kind": "ticket"})
    with pytest.raises(ValueError, match="unsupported payload version"):
        build_commit_kwargs({"version": 99, "result_key": "x", "kind": "ticket",
                             "player_id": 1, "kit_id": 1, "new_tier_id": 1,
                             "discord_role_id": 1})


async def test_default_stale_cutoff_is_aware():
    cutoff = default_stale_cutoff()
    age = datetime.now(timezone.utc) - cutoff
    assert cutoff.tzinfo is not None
    assert timedelta(minutes=4) < age <= timedelta(minutes=6)


def _unresolved_payload(*, result_key, kit_key="ht3", new_tier_code="t2", **overrides):
    """C2 audit fix payload shape: raw identifiers, not numeric FKs — see
    db/services/promotion._raw_wedge_payload."""
    now = datetime.now(timezone.utc)
    payload = {
        "version": 1,
        "resolved": False,
        "result_key": result_key,
        "kind": "ticket",
        "discord_id": 1111,
        "ign": "Promo",
        "kit_key": kit_key,
        "new_tier_code": new_tier_code,
        "discord_role_id": 777777,
        "previous_tier_code": None,
        "bridge_tier_code": None,
        "eval_flag": False,
        "recorded_at": now.isoformat(),
        "date": "2026-09-25",
        "cooldowns": [],
        "audit_actor_id": 42,
        "audit_actor_name": "Mod",
    }
    payload.update(overrides)
    return payload


async def test_consume_unresolved_payload_retries_then_succeeds_once_dimension_exists(
    session_factory, clean_db
):
    """C2 audit fix, end-to-end: Discord mutated but the kit didn't exist
    yet in PostgreSQL at promotion time (commit_promotion_with_wedge wedges
    an UNRESOLVED payload instead of silently dropping the event). The
    consumer retries and fails while the kit is still missing, then
    succeeds automatically as soon as an admin registers it — no manual
    payload surgery required, unlike the old resolved-FK-only wedge shape
    which couldn't represent this case at all."""
    from db.repositories.kits import ensure_dimensions

    async with transaction(session_factory) as session:
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Promo"
        )

    async with transaction(session_factory) as session:
        await OutboxRepository().enqueue(
            session,
            event_type="promotion_commit",
            aggregate_type="result",
            aggregate_id="c2-unresolved",
            payload=_unresolved_payload(
                result_key="c2-unresolved", kit_key="ht3", new_tier_code="t2"
            ),
            discord_role_confirmed=True,
        )

    # Kit "ht3"/tier "t2" don't exist yet — replay must retry, not crash or
    # silently drop the event.
    first = await OutboxConsumer().consume_one(session_factory)
    assert first is not None and first.outcome == CONSUMED_RETRY

    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status=OUTBOX_PENDING)
    assert len(events) == 1 and events[0].attempts == 1

    await _skip_backoff(session_factory)
    # Admin registers the missing dimensions.
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t2", "ladder", "Tier 2", 2),),
        )

    second = await OutboxConsumer().consume_one(session_factory)
    assert second is not None and second.outcome == CONSUMED_DONE

    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "c2-unresolved")
            )
        ).scalar_one()
        mirror = await MirrorRepository().get_current(
            session, player_id=player.id, kit_id=result.kit_id
        )
    assert result.promotion_status == "committed"
    assert mirror is not None and mirror.source == "promotion"

async def test_failed_event_waits_out_backoff_instead_of_burning_all_attempts(
    session_factory, clean_db
):
    await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-backoff",
        payload={"result_key": "promo-backoff", "kind": "ticket"},
        discord_role_confirmed=True,
    )
    consumed = await OutboxConsumer().consume_many(session_factory, max_events=10)
    assert [c.outcome for c in consumed] == [CONSUMED_RETRY]

    async with transaction(session_factory) as session:
        event = (await session.execute(select(OutboxEvent))).scalar_one()
    assert event.status == OUTBOX_PENDING
    assert event.attempts == 1
    assert event.next_attempt_at > datetime.now(timezone.utc)

    await _skip_backoff(session_factory)
    again = await OutboxConsumer().consume_one(session_factory)
    assert again is not None and again.attempts == 2


async def test_crash_looping_event_is_dead_lettered_on_reclaim(session_factory, clean_db):
    await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-poison",
        payload={"result_key": "promo-poison", "kind": "ticket"},
        discord_role_confirmed=True,
    )
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    async with transaction(session_factory) as session:
        await session.execute(
            update(OutboxEvent).values(
                status="in_progress", claimed_at=stale, attempts=5
            )
        )
    assert (
        await OutboxConsumer().consume_one(
            session_factory, in_progress_before=default_stale_cutoff()
        )
        is None
    )
    async with transaction(session_factory) as session:
        dead = await OutboxRepository().list_by_status(
            session, status=OUTBOX_DEAD_LETTER
        )
    assert [e.aggregate_id for e in dead] == ["promo-poison"]

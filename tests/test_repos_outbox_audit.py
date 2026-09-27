"""Outbox, sync-run, sync-action, audit, bot-config, migration-issue tests."""

from datetime import datetime, timedelta, timezone


from db.repositories.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_IN_PROGRESS,
    OUTBOX_PENDING,
    OutboxRepository,
)
from db.repositories.sync_audit import (
    AuditRepository,
    BotConfigRepository,
    MigrationIssueRepository,
    SyncActionRepository,
    SyncRunRepository,
)
from db.services.session import transaction


async def test_outbox_enqueue_claim_done(session_factory, clean_db):
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        ev = await repo.enqueue(
            session,
            event_type="promotion_commit",
            aggregate_type="result",
            aggregate_id="r-1",
            payload={"result_key": "r-1"},
            discord_role_confirmed=True,
        )
        assert ev.status == OUTBOX_PENDING
        assert ev.discord_role_confirmed is True
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        claimed = await repo.claim_next(session)
        assert claimed is not None and claimed.id == ev.id
        assert claimed.status == "in_progress"
        done = await repo.mark_done(session, event_id=ev.id)
        assert done is True
        blocked = await repo.claim_next(session)
    assert blocked is None


async def test_outbox_claim_order_and_type_filter(session_factory, clean_db):
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        older = await repo.enqueue(
            session, event_type="a", aggregate_type="r", aggregate_id="1", payload={}
        )
        newer = await repo.enqueue(
            session, event_type="b", aggregate_type="r", aggregate_id="2", payload={}
        )
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        first = await repo.claim_next(session, event_type="a")
        assert first is not None and first.id == older.id
        second = await repo.claim_next(session)
        assert second is not None and second.id == newer.id
    assert older.created_at <= newer.created_at


async def test_outbox_mark_failed_retries_then_dead_letters(session_factory, clean_db):
    async with transaction(session_factory) as session:
        ev = await OutboxRepository().enqueue(
            session, event_type="x", aggregate_type="r", aggregate_id="3", payload={}
        )
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        status_1 = await repo.mark_failed(session, event_id=ev.id, error="boom 1")
        assert status_1 == OUTBOX_PENDING
        status_2 = await repo.mark_failed(session, event_id=ev.id, error="boom 2")
        assert status_2 == OUTBOX_PENDING
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        event = await repo.claim_next(session)
        assert event is not None
        status_3 = await repo.mark_failed(session, event_id=ev.id, error="boom 3")
        assert status_3 == OUTBOX_PENDING
        status_4 = await repo.mark_failed(session, event_id=ev.id, error="boom 4")
        assert status_4 == OUTBOX_PENDING
        status_5 = await repo.mark_failed(session, event_id=ev.id, error="boom 5")
        assert status_5 == OUTBOX_DEAD_LETTER
        dead = await repo.list_by_status(session, status=OUTBOX_DEAD_LETTER)
    assert [e.id for e in dead] == [ev.id]


async def test_outbox_stale_in_progress_reclaim(session_factory, clean_db):
    stale_before = datetime.now(timezone.utc) - timedelta(hours=2)
    async with transaction(session_factory) as session:
        ev = await OutboxRepository().enqueue(
            session, event_type="z", aggregate_type="r", aggregate_id="9", payload={}
        )
        # M7 audit fix: staleness is measured from claimed_at, not created_at.
        ev.claimed_at = stale_before - timedelta(minutes=5)
        ev.status = OUTBOX_IN_PROGRESS
        await session.flush()
    async with transaction(session_factory) as session:
        repo = OutboxRepository()
        reclaimed = await repo.claim_next(
            session, in_progress_before=stale_before
        )
    assert reclaimed is not None and reclaimed.id == ev.id


async def test_outbox_backlogged_then_freshly_claimed_is_not_reclaimed(
    session_factory, clean_db
):
    """M7 audit fix regression: an event that sat `pending` for a long time
    (old created_at) but was just claimed (fresh claimed_at) must NOT be
    treated as stale — only claim age matters, not enqueue age. Before the
    fix, claim_next() compared in_progress_before against created_at, so
    this exact event would have been immediately "reclaimable" by a second
    concurrent consumer despite being actively processed."""
    stale_before = datetime.now(timezone.utc) - timedelta(hours=2)
    async with transaction(session_factory) as session:
        ev = await OutboxRepository().enqueue(
            session, event_type="z", aggregate_type="r", aggregate_id="10", payload={}
        )
        ev.created_at = stale_before - timedelta(days=1)  # backlogged for a day
        ev.status = OUTBOX_IN_PROGRESS
        ev.claimed_at = datetime.now(timezone.utc)  # claimed just now
        await session.flush()
    async with transaction(session_factory) as session:
        reclaimed = await OutboxRepository().claim_next(
            session, in_progress_before=stale_before
        )
    assert reclaimed is None, (
        "a fresh claim on a backlogged event must not be stolen by a "
        "concurrent reclaim pass"
    )


async def test_sync_run_lifecycle(session_factory, clean_db):
    from db.repositories.players import PlayerRepository

    repo = PlayerRepository()
    async with transaction(session_factory) as session:
        _, p1 = await repo.claim_discord_id(session, discord_id=21, ign="P1")
        _, p2 = await repo.claim_discord_id(session, discord_id=22, ign="P2")
    async with transaction(session_factory) as session:
        run_repo = SyncRunRepository()
        run = await run_repo.start(
            session, command="/sync discord", mode="preview", triggered_by=42
        )
        actions = SyncActionRepository()
        await actions.record(
            session, sync_run_id=run.id, action_type="observe",
            player_id=p1.id, anomaly_category=None, status="applied"
        )
        await actions.record(
            session, sync_run_id=run.id, action_type="observe",
            player_id=p2.id, anomaly_category="missing_tier", status="anomaly"
        )
        await run_repo.finish(
            session, sync_run_id=run.id, status="success",
            summary={"scanned": 2, "anomalies": 1}
        )
    async with transaction(session_factory) as session:
        runs = await SyncRunRepository().list(session, command="/sync discord")
        recorded = await SyncActionRepository().list_for_run(
            session, sync_run_id=runs[0].id
        )
    assert runs[0].status == "success"
    assert runs[0].summary["anomalies"] == 1
    assert len(recorded) == 2
    assert {a.status for a in recorded} == {"applied", "anomaly"}


async def test_audit_append_only(session_factory, clean_db):
    async with transaction(session_factory) as session:
        repo = AuditRepository()
        await repo.append(session, action="test", actor_id=1,
                          entity_type="player", entity_id="5", details={"x": 1})
        rows = await repo.list(session, entity_type="player", entity_id="5")
    assert len(rows) == 1
    assert not hasattr(AuditRepository(), "update")
    assert not hasattr(AuditRepository(), "delete")


async def test_bot_config_upsert(session_factory, clean_db):
    async with transaction(session_factory) as session:
        repo = BotConfigRepository()
        await repo.set(session, "some_flag", {"enabled": True})
        await repo.set(session, "some_flag", {"enabled": False, "note": "off"})
        value = await repo.get(session, "some_flag")
        missing = await repo.get(session, "nope", default="dflt")
    assert value == {"enabled": False, "note": "off"}
    assert missing == "dflt"


async def test_migration_issue_record_and_list(session_factory, clean_db):
    async with transaction(session_factory) as session:
        repo = MigrationIssueRepository()
        await repo.record(
            session, category="unknown_tier", source_file="players.json",
            reason="no matching tier definition", payload={"ign": "X"},
            source_key="X.2024",
        )
        open_issues = await repo.list_open(session, category="unknown_tier")
    assert len(open_issues) == 1
    assert open_issues[0].source_key == "X.2024"
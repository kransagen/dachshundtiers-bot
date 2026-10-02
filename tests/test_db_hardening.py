"""Audit fixes in the DB layer: anomaly dedup + health, retention, reconciliation resilience."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from db.models import AuditLog, OutboxEvent, SyncAction
from db.repositories.sync_audit import AuditRepository
from db.services.health import DEGRADED, HEALTHY, build_health_report
from db.services.mirror_sync import IN_CLAUSE_BATCH, DiscordSyncService, _batched
from db.services.reconciliation import ReconciliationService
from db.services.retention import purge_old_records
from db.services.session import transaction

from tests.test_phase_e_health import _seed as _seed_health
from tests.test_services_mirror_sync import _seed as _seed_mirror


@dataclass(frozen=True)
class MemberView:
    id: int
    role_ids: tuple[int, ...]


async def _anomaly_rows(session_factory):
    async with transaction(session_factory) as session:
        return (
            await session.execute(
                select(func.count(SyncAction.id)).where(SyncAction.status == "anomaly")
            )
        ).scalar_one()


def test_batched_splits_in_clause_parameters():
    assert list(_batched(list(range(7)), 3)) == [[0, 1, 2], [3, 4, 5], [6]]
    assert list(_batched([], 3)) == []
    assert IN_CLAUSE_BATCH <= 5000


async def test_sync_guild_loads_players_across_batches(session_factory, clean_db, monkeypatch):
    import db.services.mirror_sync as mirror_sync

    monkeypatch.setattr(
        mirror_sync,
        "_batched",
        lambda items, size=2: (items[i : i + size] for i in range(0, len(items), size)),
    )
    seeded = await _seed_mirror(session_factory)
    members = [MemberView(id=i, role_ids=(seeded["role_id"],)) for i in (1111, 2, 3, 4, 5)]
    outcome = await DiscordSyncService().sync_guild(session_factory, members=members)
    assert outcome.scanned_members == 5
    assert outcome.observations_applied == 1
    assert outcome.unknown_members == 4


async def test_automatic_sync_does_not_rewrite_known_anomalies(session_factory, clean_db):
    seeded = await _seed_mirror(session_factory)
    members = [MemberView(id=9999, role_ids=(seeded["role_id"],))]
    service = DiscordSyncService()

    first = await service.sync_guild(session_factory, members=members, dedupe_anomalies=True)
    second = await service.sync_guild(session_factory, members=members, dedupe_anomalies=True)

    assert first.anomalies == second.anomalies == 1
    assert await _anomaly_rows(session_factory) == 1


async def test_manual_sync_still_records_every_anomaly(session_factory, clean_db):
    seeded = await _seed_mirror(session_factory)
    members = [MemberView(id=9999, role_ids=(seeded["role_id"],))]
    service = DiscordSyncService()
    await service.sync_guild(session_factory, members=members)
    await service.sync_guild(session_factory, members=members)
    assert await _anomaly_rows(session_factory) == 2


async def test_health_counts_anomalies_of_latest_run_only(session_factory, clean_db):
    seeded = await _seed_mirror(session_factory)
    service = DiscordSyncService()
    await service.sync_guild(
        session_factory,
        members=[MemberView(id=9999, role_ids=(seeded["role_id"],))],
        dedupe_anomalies=True,
    )
    await service.sync_guild(
        session_factory,
        members=[MemberView(id=9999, role_ids=(seeded["role_id"],))],
        dedupe_anomalies=True,
    )
    checks = {c["key"]: c for c in (await build_health_report(session_factory))["checks"]}
    assert checks["sync_anomalies"]["status"] == DEGRADED
    assert checks["sync_anomalies"]["detail"].startswith("1 ")

    await service.sync_guild(
        session_factory, members=[MemberView(id=9999, role_ids=())], dedupe_anomalies=True
    )
    checks = {c["key"]: c for c in (await build_health_report(session_factory))["checks"]}
    assert checks["sync_anomalies"]["status"] == HEALTHY


async def test_purge_old_records_keeps_recent_and_unfinished(session_factory, clean_db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    seeded = await _seed_mirror(session_factory)
    await DiscordSyncService().sync_guild(
        session_factory, members=[MemberView(id=9999, role_ids=(seeded["role_id"],))]
    )
    async with transaction(session_factory) as session:
        await session.execute(update(SyncAction).values(created_at=old))
        old_audit = await AuditRepository().append(session, action="old_thing")
        old_audit.created_at = old
        await AuditRepository().append(session, action="recent_thing")
        for status in ("done", "done", "dead_letter"):
            session.add(
                OutboxEvent(
                    event_type="t", aggregate_type="r", aggregate_id=f"x-{status}",
                    payload={}, status=status, processed_at=old,
                )
            )
        session.add(
            OutboxEvent(
                event_type="t", aggregate_type="r", aggregate_id="fresh",
                payload={}, status="done", processed_at=now,
            )
        )
        await session.flush()

    outcome = await purge_old_records(session_factory, now=now)

    assert outcome.sync_actions == 1
    assert outcome.audit_logs == 1
    assert outcome.outbox_events == 2
    async with transaction(session_factory) as session:
        actions = {a for (a,) in (await session.execute(select(AuditLog.action))).all()}
        statuses = sorted(s for (s,) in (await session.execute(select(OutboxEvent.status))).all())
    assert "old_thing" not in actions and "recent_thing" in actions
    assert statuses == ["dead_letter", "done"]


async def test_reconcile_still_syncs_when_outbox_consumption_fails(session_factory, clean_db):
    seeded = await _seed_mirror(session_factory)

    class BrokenConsumer:
        async def consume_many(self, *args, **kwargs):
            raise RuntimeError("db hiccup")

    outcome = await ReconciliationService(consumer=BrokenConsumer()).reconcile(
        session_factory, members=[MemberView(id=1111, role_ids=(seeded["role_id"],))]
    )
    assert outcome.outbox_consumed == ()
    assert outcome.sync.observations_applied == 1


async def test_reconcile_survives_retention_failure(session_factory, clean_db, monkeypatch):
    import db.services.reconciliation as reconciliation

    async def boom(*args, **kwargs):
        raise RuntimeError("purge failed")

    monkeypatch.setattr(reconciliation, "purge_old_records", boom)
    await _seed_health(session_factory, ign="Purge", observed_at=datetime.now(timezone.utc))
    outcome = await ReconciliationService().reconcile(session_factory, members=[])
    assert outcome.sync.scanned_members == 0

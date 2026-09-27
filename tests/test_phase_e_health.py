"""Phase E, E4 — mirror health report tests.

Verifies HEALTHY / DEGRADED / FAILED aggregation: fresh observations and
clean outbox stay healthy; stale observation, dead-letter backlog, open
migration issues and recent DB failures degrade or fail the report.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import MigrationImportIssue, OutboxEvent, PlayerCurrentTier
from db.repositories.outbox import OUTBOX_DEAD_LETTER
from db.repositories.sync_audit import SYNC_RUN_SUCCESS, AuditRepository, SyncRunRepository
from db.services.health import (
    DEGRADED,
    FAILED,
    HEALTHY,
    build_health_report,
    render_markdown_report,
)
from db.services.session import transaction


async def _seed(session_factory, *, ign, observed_at):
    from db.models import Kit, TierDefinition
    from db.repositories.kits import ensure_dimensions
    from db.repositories.players import PlayerRepository

    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        tier = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "t2")
        )).scalar_one()
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign=ign
        )
        run = await SyncRunRepository().start(session, command="sync_discord", mode="apply")
        await SyncRunRepository().finish(session, sync_run_id=run.id, status=SYNC_RUN_SUCCESS)
        session.add(
            PlayerCurrentTier(
                player_id=player.id,
                kit_id=kit.id,
                tier_id=tier.id,
                discord_role_id=777777,
                observed_at=observed_at,
                source="discord_sync",
            )
        )
        await session.flush()
        return {"player": player, "kit": kit, "tier": tier}


async def test_healthy_report(session_factory, clean_db):
    await _seed(
        session_factory,
        ign="Health",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    report = await build_health_report(session_factory)
    assert report["status"] == HEALTHY
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["observation_freshness"] == HEALTHY
    assert checks["last_mirror_sync"] == HEALTHY
    assert checks["unresolved_identities"] == HEALTHY
    assert checks["outbox_backlog"] == HEALTHY
    assert checks["recent_db_failures"] == HEALTHY
    assert "mirror healthy" in report["conclusion"]


async def test_stale_observation_fails_report(session_factory, clean_db):
    await _seed(
        session_factory,
        ign="Stale",
        observed_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    report = await build_health_report(session_factory)
    assert report["status"] == FAILED
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["observation_freshness"] == FAILED


async def test_dead_letter_backlog_fails_report(session_factory, clean_db):
    await _seed(
        session_factory,
        ign="Backlog",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    async with transaction(session_factory) as session:
        session.add(
            OutboxEvent(
                event_type="promotion_commit",
                aggregate_type="result",
                aggregate_id="bk-1",
                payload={"result_key": "bk-1"},
                status=OUTBOX_DEAD_LETTER,
                discord_role_confirmed=False,
                attempts=5,
            )
        )
        await session.flush()
    report = await build_health_report(session_factory)
    assert report["status"] == FAILED
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["outbox_backlog"] == FAILED


async def test_open_issue_and_recent_failure_degrade_report(session_factory, clean_db):
    await _seed(
        session_factory,
        ign="Degraded",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    async with transaction(session_factory) as session:
        session.add(
            MigrationImportIssue(
                category="identity",
                source_file="players.json",
                reason="no discord link",
                payload={"ign": "X"},
            )
        )
        await AuditRepository().append(
            session, action="outbox_failed", actor_id=None, details={"error": "boom"}
        )
        await session.flush()
    report = await build_health_report(session_factory)
    assert report["status"] == DEGRADED
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["unresolved_identities"] == DEGRADED
    assert checks["recent_db_failures"] == DEGRADED


async def test_healthy_report_includes_new_m1_checks(session_factory, clean_db):
    """M1 audit fix: stale outbox claims and stuck discord_pending
    promotions must be present in every report, not just visible when bad."""
    await _seed(
        session_factory,
        ign="M1Healthy",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    report = await build_health_report(session_factory)
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["outbox_stale_claims"] == HEALTHY
    assert checks["unresolved_promotions"] == HEALTHY


async def test_stale_outbox_claim_fails_report(session_factory, clean_db):
    """M1 audit fix: a claim held past the stale-lock window (the exact
    scenario C1 fixed reconciliation to reclaim) must be operator-visible in
    the health report, not just silently reclaimed on the next pass."""
    from db.repositories.outbox import OUTBOX_IN_PROGRESS

    await _seed(
        session_factory,
        ign="StaleClaim",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    async with transaction(session_factory) as session:
        session.add(
            OutboxEvent(
                event_type="promotion_commit",
                aggregate_type="result",
                aggregate_id="stale-claim-1",
                payload={"result_key": "stale-claim-1"},
                status=OUTBOX_IN_PROGRESS,
                claimed_at=datetime.now(timezone.utc) - timedelta(hours=2),
                discord_role_confirmed=True,
            )
        )
        await session.flush()
    report = await build_health_report(session_factory)
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["outbox_stale_claims"] == FAILED
    assert report["status"] == FAILED


async def test_stuck_discord_pending_promotion_degrades_report(session_factory, clean_db):
    """M1 audit fix (closes D3): a Result stuck at discord_pending past the
    grace window means the Discord role grant never completed and nothing
    else will ever surface it — this must show up in health."""
    from db.models import Result

    seeded = await _seed(
        session_factory,
        ign="StuckPromo",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    async with transaction(session_factory) as session:
        session.add(
            Result(
                result_key="stuck-1",
                kind="ticket",
                player_id=seeded["player"].id,
                kit_id=seeded["kit"].id,
                new_tier_id=seeded["tier"].id,
                promotion_status="discord_pending",
                recorded_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
        await session.flush()
    report = await build_health_report(session_factory)
    checks = {c["key"]: c["status"] for c in report["checks"]}
    assert checks["unresolved_promotions"] == DEGRADED


async def test_markdown_report_lists_every_check(session_factory, clean_db):
    await _seed(
        session_factory,
        ign="Markdown",
        observed_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    report = await build_health_report(session_factory)
    rendered = render_markdown_report(report)
    assert "Mirror health: `healthy`" in rendered
    for check in report["checks"]:
        assert check["label"] in rendered
    assert report["conclusion"] in rendered
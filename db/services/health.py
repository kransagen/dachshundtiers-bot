"""Phase E, E4 — PostgreSQL mirror health report.

Observe-only. Reads ``player_current_tiers`` freshness, mirror sync runs,
migration issues, outbox backlog, sync anomalies and recently failed DB ops;
aggregates into HEALTHY / DEGRADED / FAILED. Never mutates anything.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db.models import (
    AuditLog,
    MigrationImportIssue,
    OutboxEvent,
    PlayerCurrentTier,
    Result,
    SyncAction,
    SyncRun,
)
from db.repositories.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_IN_PROGRESS,
    OUTBOX_PENDING,
)
from db.repositories.results import PROMOTION_DISCORD_PENDING
from db.repositories.sync_audit import SYNC_ACTION_ANOMALY, SYNC_RUN_SUCCESS
from db.services.outbox_consumer import default_stale_cutoff
from db.services.session import transaction

HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "failed"

OBSERVATION_WARN_AFTER = timedelta(hours=24)
OBSERVATION_FAIL_AFTER = timedelta(days=3)
OUTBOX_WARN_MIN = 2
DB_FAILURE_LOOKBACK = timedelta(hours=24)
# M1 audit fix: how old an unresolved/discord_pending promotion Result must
# be before it's flagged — a brand-new one is normal (Discord mutation and
# the mirror commit are two separate steps in the same command, a few
# milliseconds apart); one still pending after this long means the grant
# failed (or crashed) and nothing ever wedged/committed it.
STUCK_PROMOTION_AFTER = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HealthCheck:
    key: str
    label: str
    status: str
    detail: str


async def build_health_report(
    session_factory,
    *,
    observation_warn_after: timedelta = OBSERVATION_WARN_AFTER,
    observation_fail_after: timedelta = OBSERVATION_FAIL_AFTER,
) -> dict:
    now = _utcnow()
    checks: list[HealthCheck] = []

    async with transaction(session_factory) as session:
        latest_obs = (
            await session.execute(
                select(func.max(PlayerCurrentTier.observed_at))
            )
        ).scalar()
        if latest_obs is None:
            checks.append(
                HealthCheck(
                    key="observation_freshness",
                    label="Discord observation age",
                    status=FAILED,
                    detail="no mirror observation recorded",
                )
            )
        else:
            age = now - latest_obs
            if age > observation_fail_after:
                checks.append(
                    HealthCheck(
                        key="observation_freshness",
                        label="Discord observation age",
                        status=FAILED,
                        detail=f"last observation {latest_obs.isoformat()} ({age})",
                    )
                )
            elif age > observation_warn_after:
                checks.append(
                    HealthCheck(
                        key="observation_freshness",
                        label="Discord observation age",
                        status=DEGRADED,
                        detail=f"last observation {latest_obs.isoformat()} ({age})",
                    )
                )
            else:
                checks.append(
                    HealthCheck(
                        key="observation_freshness",
                        label="Discord observation age",
                        status=HEALTHY,
                        detail=f"last observation {latest_obs.isoformat()} ({age})",
                    )
                )

        last_success = (
            await session.execute(
                select(SyncRun)
                .where(SyncRun.status == SYNC_RUN_SUCCESS)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_success is None:
            checks.append(
                HealthCheck(
                    key="last_mirror_sync",
                    label="Last mirror sync",
                    status=DEGRADED,
                    detail="no successful sync run recorded",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    key="last_mirror_sync",
                    label="Last mirror sync",
                    status=HEALTHY,
                    detail=f"{last_success.command} {last_success.started_at.isoformat()}",
                )
            )

        unresolved = (
            await session.execute(
                select(func.count(MigrationImportIssue.id)).where(
                    MigrationImportIssue.status == "open"
                )
            )
        ).scalar()
        checks.append(
            HealthCheck(
                key="unresolved_identities",
                label="Unresolved migration issues",
                status=DEGRADED if unresolved else HEALTHY,
                detail=f"{unresolved} open",
            )
        )

        anomalies = (
            await session.execute(
                select(func.count(SyncAction.id)).where(
                    SyncAction.status == SYNC_ACTION_ANOMALY
                )
            )
        ).scalar()
        checks.append(
            HealthCheck(
                key="sync_anomalies",
                label="Sync anomalies",
                status=DEGRADED if anomalies else HEALTHY,
                detail=f"{anomalies} anomaly actions",
            )
        )

        pending = (
            await session.execute(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.status.in_(
                        (OUTBOX_PENDING, OUTBOX_IN_PROGRESS)
                    )
                )
            )
        ).scalar()
        dead = (
            await session.execute(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.status == OUTBOX_DEAD_LETTER
                )
            )
        ).scalar()
        checks.append(
            HealthCheck(
                key="outbox_backlog",
                label="Outbox backlog",
                status=(
                    FAILED
                    if dead
                    else DEGRADED if pending >= OUTBOX_WARN_MIN else HEALTHY
                ),
                detail=(
                    f"{pending} pending, {dead} dead-letter"
                    if dead
                    else f"{pending} pending/in-progress"
                ),
            )
        )

        # M1 audit fix: distinguish a claim that's merely being actively
        # processed from one that's been held past the stale-lock window —
        # the latter means a consumer crashed mid-replay and, before the C1
        # fix, would have been invisible forever. Surfacing it here makes a
        # reconciliation gap operator-visible instead of silent.
        stale_cutoff = default_stale_cutoff()
        stale_claims = (
            await session.execute(
                select(func.count(OutboxEvent.id)).where(
                    OutboxEvent.status == OUTBOX_IN_PROGRESS,
                    OutboxEvent.claimed_at.is_not(None),
                    OutboxEvent.claimed_at < stale_cutoff,
                )
            )
        ).scalar()
        checks.append(
            HealthCheck(
                key="outbox_stale_claims",
                label="Outbox stale in-progress claims",
                status=FAILED if stale_claims else HEALTHY,
                detail=(
                    f"{stale_claims} claimed before {stale_cutoff.isoformat()} "
                    "(will be reclaimed on next reconciliation pass)"
                    if stale_claims
                    else "none"
                ),
            )
        )

        # M1 audit fix (also closes D3): a Result stuck at discord_pending
        # means the Discord role grant that should have followed the /result
        # write never succeeded (or the process crashed before attempting
        # it) — commit_promotion_with_wedge is only ever called when the
        # grant succeeds, so this row was never even eligible for a wedge.
        # Previously invisible: list_pending_commits() had zero production
        # callers.
        stuck_cutoff = now - STUCK_PROMOTION_AFTER
        stuck_promotions = (
            await session.execute(
                select(func.count(Result.id)).where(
                    Result.promotion_status == PROMOTION_DISCORD_PENDING,
                    Result.recorded_at < stuck_cutoff,
                )
            )
        ).scalar()
        checks.append(
            HealthCheck(
                key="unresolved_promotions",
                label="Unresolved (discord_pending) promotions",
                status=DEGRADED if stuck_promotions else HEALTHY,
                detail=(
                    f"{stuck_promotions} stuck older than {STUCK_PROMOTION_AFTER} "
                    "— Discord role grant likely never succeeded; needs manual review"
                    if stuck_promotions
                    else "none"
                ),
            )
        )

        recent_failures = (
            await session.execute(
                select(func.count(AuditLog.id)).where(
                    AuditLog.action.like("%_failed"),
                    AuditLog.created_at >= now - DB_FAILURE_LOOKBACK,
                )
            )
        ).scalar()
        checks.append(
            HealthCheck(
                key="recent_db_failures",
                label="Failed DB ops (24h)",
                status=DEGRADED if recent_failures else HEALTHY,
                detail=f"{recent_failures} failure audit rows",
            )
        )

    failed = [c for c in checks if c.status == FAILED]
    degraded = [c for c in checks if c.status == DEGRADED]
    overall = (
        FAILED if failed else DEGRADED if degraded else HEALTHY
    )
    return {
        "status": overall,
        "generated_at": now.isoformat(),
        "checks": [asdict(c) for c in checks],
        "conclusion": (
            "mirror healthy; Discord observations current, no backlog"
            if overall == HEALTHY
            else "mirror degraded; see checks"
            if overall == DEGRADED
            else "mirror FAILED; immediate attention required"
        ),
    }


def render_markdown_report(report: dict) -> str:
    lines = [
        f"# Mirror health: `{report['status']}`",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "| Check | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        lines.append(
            f"| {check['label']} | `{check['status']}` | {check['detail']} |"
        )
    lines.append("")
    lines.append(f"{report['conclusion']}")
    return "\n".join(lines)
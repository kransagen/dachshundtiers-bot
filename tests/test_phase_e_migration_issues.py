"""Phase E, E9 — migration issues are classified, never silently resolved.

The classification report groups the granular import-issue categories into 6
actionable buckets. Hard contract: the report is READ-ONLY (never resolves,
never guesses, never mutates Discord) and every issue stays open until an
explicit operator actions it (/linkdiscord or corrected source re-import).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from db.models import MigrationImportIssue
from db.repositories.sync_audit import MigrationIssueRepository

from services.phase_e.migration_issues import (
    ACTIONABLE_RECOMMENDATION,
    BUCKET_COOLDOWN,
    BUCKET_HISTORY,
    BUCKET_HT3,
    BUCKET_OTHER,
    BUCKET_PLAYER,
    BUCKET_TESTER,
    BUCKETS,
    build_migration_issues_report,
)
from services.phase_d.import_data import (
    CAT_COOLDOWN_UNRESOLVED,
    CAT_HISTORY_INVALID_DATE,
    CAT_HT3_UNRESOLVED,
    CAT_PLAYER_MALFORMED,
    CAT_TESTER_UNRESOLVED,
)


async def _count_open(session_factory) -> int:
    async with session_factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(MigrationImportIssue)
                .where(MigrationImportIssue.status == "open")
            )
        ).scalar_one()


async def _seed_issues(session_factory) -> None:
    repo = MigrationIssueRepository()
    async with session_factory() as session:
        for category, key in [
            (CAT_PLAYER_MALFORMED, "record:0"),
            (CAT_PLAYER_MALFORMED, "record:1"),
            (CAT_HISTORY_INVALID_DATE, None),
            (CAT_COOLDOWN_UNRESOLVED, None),
            (CAT_HT3_UNRESOLVED, None),
            (CAT_TESTER_UNRESOLVED, "999999999999999999"),
        ]:
            await repo.record(
                session,
                category=category,
                source_file="players.json",
                source_key=key,
                reason="Test fixture issue",
                payload={"username": "Atrajmix_"},
            )
        await session.commit()


@pytest.mark.asyncio
async def test_report_groups_all_13_categories_into_6_buckets(
    session_factory, clean_db
):
    await _seed_issues(session_factory)
    report = await build_migration_issues_report(session_factory)

    assert report["summary"]["bucket_count"] == 6
    assert len(report["buckets"]) == 6
    assert report["summary"]["open_issues"] == 6

    by_name = {b["name"]: b for b in report["buckets"]}
    assert by_name[BUCKET_PLAYER["name"]]["count"] == 2
    assert by_name[BUCKET_HISTORY["name"]]["count"] == 1
    assert by_name[BUCKET_COOLDOWN["name"]]["count"] == 1
    assert by_name[BUCKET_HT3["name"]]["count"] == 1
    assert by_name[BUCKET_TESTER["name"]]["count"] == 1
    assert by_name[BUCKET_OTHER["name"]]["count"] == 0


@pytest.mark.asyncio
async def test_every_known_category_maps_to_exactly_one_bucket(
    session_factory, clean_db
):
    from services.phase_d import import_data

    known_categories = {
        v
        for k, v in vars(import_data).items()
        if k.startswith("CAT_") and isinstance(v, str)
    }
    mapped = {
        cat for b in BUCKETS for cat in b["categories"]
    }
    unmapped = known_categories - mapped
    assert unmapped == set(), f"Kategorie bez bucketu: {sorted(unmapped)}"

    bucket_categories = [cat for b in BUCKETS for cat in b["categories"]]
    assert len(bucket_categories) == len(set(bucket_categories)), (
        "Kategorie náleží do více než jednoho bucketu"
    )


@pytest.mark.asyncio
async def test_report_is_read_only_and_never_resolves(session_factory, clean_db):
    await _seed_issues(session_factory)
    before = await _count_open(session_factory)
    assert before == 6

    report = await build_migration_issues_report(session_factory)

    after = await _count_open(session_factory)
    assert after == before, "Report nesmí měnit stav issues"

    for bucket in report["buckets"]:
        if bucket["count"]:
            assert bucket["samples"], "Každý neprázdný bucket má vzorky"
            assert bucket["action"], "Každý bucket má akční doporučení"
    assert "linkdiscord" in ACTIONABLE_RECOMMENDATION
    assert "automaticky" in ACTIONABLE_RECOMMENDATION
    assert "nikdy" in ACTIONABLE_RECOMMENDATION


@pytest.mark.asyncio
async def test_unknown_future_category_lands_in_other_bucket_unaudited(
    session_factory, clean_db
):
    repo = MigrationIssueRepository()
    async with session_factory() as session:
        await repo.record(
            session,
            category="future_unknown_something",
            source_file="future.json",
            source_key="k",
            reason="Budoucí kategorie",
            payload={"a": 1},
        )
        await session.commit()

    report = await build_migration_issues_report(session_factory)
    other = next(b for b in report["buckets"] if b["name"] == BUCKET_OTHER["name"])
    assert other["count"] == 1
    assert "future_unknown_something" in other["categories"]
    assert other["samples"], "Neznámá kategorie se hlásí, neskrývá"
    assert other["count"] == 1


@pytest.mark.asyncio
async def test_sum_of_bucket_counts_equals_open_issues(session_factory, clean_db):
    await _seed_issues(session_factory)
    report = await build_migration_issues_report(session_factory)

    bucket_total = sum(b["count"] for b in report["buckets"])
    assert bucket_total == report["summary"]["open_issues"] == 6
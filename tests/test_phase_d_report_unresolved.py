"""D3 — unresolved-identity report tests (services.phase_d.report_unresolved).

Read-only report: no guessing, no writes, explicit per-category counts.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from db.models import AuditLog, BotConfig, Player
from db.repositories.players import PlayerRepository

from services.phase_d.import_data import (
    BOT_CONFIG_IMPORT_KEY,
    CAT_COOLDOWN_UNRESOLVED,
    CAT_HT3_UNRESOLVED,
    CAT_TESTER_UNRESOLVED,
    import_json_data,
)
from services.phase_d.report_unresolved import (
    REPORT_RECOMMENDATION,
    build_unresolved_report,
)

LINKED_DID = 111111111111111111
UNLINKED_DID = 222222222222222222
PLAYERS = [
    {
        "username": "Atrajmix_",
        "modes": {"IronAxe": "LT3"},
        "history": {
            "IronAxe": [{"date": "16.05.2026", "tier": "LT3"}]
        },
    },
    {"username": "Crityx_", "modes": {}, "history": {}},
]
COOLDOWNS = {str(UNLINKED_DID): 1780593929525}
HT3 = {str(UNLINKED_DID): {"IronAxe": 1785769543952}}
TESTERS = ["999999999999999999"]


def _write_all(data_dir) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in {
        "players.json": PLAYERS,
        "kits.json": ["IronAxe"],
        "cooldowns.json": COOLDOWNS,
        "ht3_cooldowns.json": HT3,
        "testers.json": TESTERS,
    }.items():
        (data_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


async def _link(session_factory, *, discord_id: int, ign: str) -> Player:
    async with session_factory() as session:
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=discord_id, ign=ign
        )
        await session.commit()
        return player


@pytest.mark.asyncio
async def test_report_lists_unlinked_and_categories(
    tmp_path, session_factory, clean_db
):
    _write_all(tmp_path)
    await import_json_data(session_factory, data_dir=tmp_path)

    report = await build_unresolved_report(session_factory)

    summary = report["summary"]
    assert summary["players_total"] == 2
    assert summary["players_linked"] == 0
    assert summary["players_unlinked"] == 2
    assert summary["open_issues"] == 3  # 1 cooldown + 1 ht3 + 1 tester
    assert set(summary["issue_categories"]) == {
        CAT_COOLDOWN_UNRESOLVED,
        CAT_HT3_UNRESOLVED,
        CAT_TESTER_UNRESOLVED,
    }
    assert {p["ign"] for p in report["unlinked_players"]} == {
        "Atrajmix_",
        "Crityx_",
    }
    by_cat = report["open_issues_by_category"]
    assert by_cat[CAT_COOLDOWN_UNRESOLVED]["count"] == 1
    assert by_cat[CAT_HT3_UNRESOLVED]["count"] == 1
    sample = by_cat[CAT_TESTER_UNRESOLVED]["samples"][0]
    assert sample["source_file"] == "testers.json"
    assert sample["payload"]["discord_id"] == "999999999999999999"
    assert report["recommendation"] == REPORT_RECOMMENDATION


@pytest.mark.asyncio
async def test_report_reflects_link_and_stays_read_only(
    tmp_path, session_factory, clean_db
):
    _write_all(tmp_path)
    await import_json_data(session_factory, data_dir=tmp_path)

    baseline_audit = await _count(session_factory, AuditLog)
    baseline_markers = await _count(session_factory, BotConfig)

    report = await build_unresolved_report(session_factory)
    before_link_unlinked = report["summary"]["players_unlinked"]

    await _link(session_factory, discord_id=LINKED_DID, ign="Atrajmix_")
    report = await build_unresolved_report(session_factory)

    assert before_link_unlinked == 2
    assert report["summary"]["players_linked"] == 1
    assert report["summary"]["players_unlinked"] == 1
    assert [p["ign"] for p in report["unlinked_players"]] == ["Crityx_"]

    assert await _count(session_factory, AuditLog) == baseline_audit
    assert await _count(session_factory, BotConfig) == baseline_markers
    async with session_factory() as session:
        untouched = await session.get(BotConfig, BOT_CONFIG_IMPORT_KEY)
        assert untouched is not None
        assert untouched.value["report"]["players_created"] == 2
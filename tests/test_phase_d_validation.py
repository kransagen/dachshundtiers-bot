"""D5 — tri-source validation tests (services.phase_d.report_validation).

Discord observation > mirror > players.json; conflicts reported, never fixed.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from db.models import Kit, TierDefinition
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction

from services.phase_d.report_validation import (
    CONFLICT_REASON,
    DISCORD_WINS,
    build_validation_report,
)
from services.phase_d.snapshot_tiers import SimulatedMember, run_snapshot


def _write_json(data_dir, name, payload) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


async def _seed(session_factory):
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (
                ("t1", "ladder", "Tier 1", 1),
                ("t2", "ladder", "Tier 2", 2),
                ("t3", "ladder", "Tier 3", 3),
            ),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t2 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t2")
            )
        ).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t2.id, discord_role_id=7777
        )
        _, linked = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Mirror"
        )
        unlinked = await PlayerRepository().get_or_create_by_ign(
            session, ign="NoDiscord", source="migration"
        )
        return {"kit": kit, "t2": t2, "linked": linked, "unlinked": unlinked}


async def _observe(session_factory, seeded) -> None:
    await run_snapshot(
        session_factory, members=[SimulatedMember(id=1111, role_ids=(7777,))]
    )


@pytest.mark.asyncio
async def test_json_conflict_reported_discord_wins(tmp_path, session_factory, clean_db):
    seeded = await _seed(session_factory)
    await _observe(session_factory, seeded)
    _write_json(
        tmp_path,
        "players.json",
        [{"username": "Mirror", "modes": {"ht3": "t1"}, "history": {}}],
    )
    _write_json(tmp_path, "kits.json", ["ht3"])

    report = await build_validation_report(session_factory, data_dir=tmp_path)

    assert report["discord_wins"] is DISCORD_WINS
    assert report["summary"] == {"json_conflict": 1}
    async with session_factory() as session:
        t1 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t1")
            )
        ).scalar_one()
    row = report["discrepancies"][0]
    assert row["player_ign"] == "Mirror"
    assert row["status"] == "json_conflict"
    assert row["json_tier_id"] == t1.id
    assert row["discord_tier_id"] == seeded["t2"].id
    assert row["mirror_tier_id"] == seeded["t2"].id
    assert row["reason"] == CONFLICT_REASON
    assert report["latest_sync_run"]["command"] == "/snapshot-tiers (Phase D)"


@pytest.mark.asyncio
async def test_agree_when_json_matches_discord(tmp_path, session_factory, clean_db):
    seeded = await _seed(session_factory)
    await _observe(session_factory, seeded)
    _write_json(
        tmp_path,
        "players.json",
        [{"username": "Mirror", "modes": {"ht3": "t2"}, "history": {}}],
    )
    _write_json(tmp_path, "kits.json", ["ht3"])

    report = await build_validation_report(session_factory, data_dir=tmp_path)
    assert report["summary"] == {"agree": 1}
    assert report["discrepancies"] == []


@pytest.mark.asyncio
async def test_unverified_and_unknown_code(tmp_path, session_factory, clean_db):
    seeded = await _seed(session_factory)
    await _observe(session_factory, seeded)
    _write_json(
        tmp_path,
        "players.json",
        [
            {"username": "NoDiscord", "modes": {"ht3": "t1"}, "history": {}},
            {"username": "Mirror", "modes": {"ht3": "t9"}, "history": {}},
        ],
    )
    _write_json(tmp_path, "kits.json", ["ht3"])

    report = await build_validation_report(session_factory, data_dir=tmp_path)

    assert report["summary"] == {"json_unverified": 1, "json_unknown_code": 1}
    statuses = {r["status"]: r for r in report["discrepancies"]}
    assert statuses["json_unverified"]["player_ign"] == "NoDiscord"
    assert statuses["json_unknown_code"]["json_tier_code"] == "t9"
    assert report["unlinked_players"] == 1


@pytest.mark.asyncio
async def test_missing_players_json_raises(tmp_path, session_factory, clean_db):
    with pytest.raises(ValueError):
        await build_validation_report(session_factory, data_dir=tmp_path)
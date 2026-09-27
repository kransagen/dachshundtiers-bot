"""D4 — snapshot tool tests: observe-only mirroring via run_snapshot().

Simulated guild members through services.phase_d.snapshot_tiers; never
mutates Discord (the tool has no role-mutation surface at all).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from db.models import Kit, Player, TierDefinition, TierHistory
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.sync_audit import (
    SYNC_ACTION_ANOMALY,
    SYNC_ACTION_APPLIED,
    SyncActionRepository,
    SyncRunRepository,
)
from db.repositories.tiers import MirrorRepository
from db.services.session import transaction

from services.phase_d.snapshot_tiers import (
    SNAPSHOT_COMMAND,
    SimulatedMember,
    load_members,
    run_snapshot,
)


@dataclass(frozen=True)
class MemberView:
    id: int
    role_ids: tuple[int, ...]


async def _seed(session_factory, *, role_id=7777):
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t2 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t2")
            )
        ).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t2.id, discord_role_id=role_id
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Mirror"
        )
        return {"kit": kit, "tier": t2, "player": player, "role_id": role_id}


async def test_snapshot_applies_clean_observation(session_factory, clean_db):
    seeded = await _seed(session_factory)
    report = await run_snapshot(
        session_factory,
        members=[SimulatedMember(id=1111, role_ids=(seeded["role_id"],))],
        triggered_by=42,
        triggered_by_name="Admin",
    )

    body = report.as_dict()
    assert body["command"] == SNAPSHOT_COMMAND
    assert body["discord_mutations"] == 0
    assert body["status"] == "success"
    assert body["scanned_members"] == 1
    assert body["observations_applied"] == 1
    assert body["anomalies"] == 0

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert mirror is not None
    assert mirror.tier_id == seeded["tier"].id
    assert mirror.source == "discord_sync"

    async with transaction(session_factory) as session:
        actions = await SyncActionRepository().list_for_run(
            session, sync_run_id=body["sync_run_id"]
        )
    assert len(actions) == 1
    assert actions[0].action_type == "observe"
    assert actions[0].status == SYNC_ACTION_APPLIED


async def test_snapshot_never_guesses_unknown_member(session_factory, clean_db):
    seeded = await _seed(session_factory)
    report = await run_snapshot(
        session_factory,
        members=[SimulatedMember(id=9999, role_ids=(seeded["role_id"],))],
    )

    body = report.as_dict()
    assert body["unknown_members"] == 1
    assert body["observations_applied"] == 0

    async with transaction(session_factory) as session:
        actions = await SyncActionRepository().list_for_run(
            session, sync_run_id=body["sync_run_id"]
        )
    assert len(actions) == 1
    assert actions[0].status == SYNC_ACTION_ANOMALY
    assert actions[0].anomaly_category == "unknown_player"
    assert actions[0].player_id is None

    async with session_factory() as session:
        players = (
            await session.execute(
                select(func.count()).select_from(Player)
            )
        ).scalar_one()
    assert players == 1  # only the linked one, nothing fabricated


@pytest.mark.asyncio
async def test_snapshot_second_run_updates_mirror_and_history(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        t1 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t1")
            )
        ).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=seeded["kit"].id, tier_id=t1.id, discord_role_id=7778
        )

    first = await run_snapshot(
        session_factory, members=[SimulatedMember(id=1111, role_ids=(7778,))]
    )
    assert first.as_dict()["observations_applied"] == 1

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        runs = await SyncRunRepository().list(session, limit=1)
    assert mirror.tier_id == t1.id
    assert runs[0].command == SNAPSHOT_COMMAND

    async with session_factory() as session:
        history = (
            await session.execute(
                select(TierHistory).where(TierHistory.source == "discord_sync")
            )
        ).scalars().all()
    assert len(history) == 1
    assert history[0].kit_id == seeded["kit"].id


def test_load_members_validates_input(tmp_path):
    path = tmp_path / "members.json"
    path.write_text(
        json.dumps([{"id": "1111", "role_ids": [1, 2]}, {"id": 2222}]),
        encoding="utf-8",
    )
    members = load_members(path)
    assert members == [
        SimulatedMember(id=1111, role_ids=(1, 2)),
        SimulatedMember(id=2222, role_ids=()),
    ]

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"id": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_members(bad)
"""DiscordSyncService tests — observe-only mirroring, zero Discord mutations."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuditLog, Kit, TierDefinition
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.sync_audit import (
    SYNC_ACTION_ANOMALY,
    SYNC_ACTION_APPLIED,
    SYNC_ACTION_FAILED,
    SYNC_RUN_FAILED,
    SYNC_RUN_PARTIAL,
    SYNC_RUN_SUCCESS,
    SyncActionRepository,
    SyncRunRepository,
)
from db.repositories.tiers import MirrorRepository, MirrorServiceRepository
from db.services.mirror_sync import (
    SYNC_ANOMALY_UNKNOWN_PLAYER,
    DiscordSyncService,
)
from db.services.session import transaction


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
        t2 = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "t2")
        )).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t2.id, discord_role_id=role_id
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Mirror"
        )
        return {"kit": kit, "tier": t2, "player": player, "role_id": role_id}


async def _run_summary(session_factory, sync_run_id):
    async with transaction(session_factory) as session:
        run = await SyncRunRepository().get(session, sync_run_id)
        actions = await SyncActionRepository().list_for_run(
            session, sync_run_id=sync_run_id
        )
        return run, actions


async def test_sync_guild_applies_clean_observation(session_factory, clean_db):
    seeded = await _seed(session_factory)
    members = [MemberView(id=1111, role_ids=(seeded["role_id"],))]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members, triggered_by=42, triggered_by_name="Mod"
    )

    assert outcome.status == SYNC_RUN_SUCCESS
    assert outcome.scanned_members == 1
    assert outcome.observations_applied == 1
    assert outcome.anomalies == 0
    assert outcome.unknown_roles == ()

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert mirror is not None
    assert mirror.tier_id == seeded["tier"].id
    assert mirror.source == "discord_sync"
    assert mirror.discord_role_id == seeded["role_id"]
    assert any(a.action == "sync_discord_observe" for a in audits)

    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert run.status == SYNC_RUN_SUCCESS
    assert run.summary["applied"] == 1
    assert len(actions) == 1
    assert actions[0].status == SYNC_ACTION_APPLIED
    assert actions[0].player_id == seeded["player"].id


async def test_sync_guild_unknown_player_is_anomaly_not_created(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    members = [MemberView(id=9999, role_ids=(seeded["role_id"],))]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members
    )

    assert outcome.status == SYNC_RUN_SUCCESS
    assert outcome.unknown_members == 1
    assert outcome.observations_applied == 0

    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert len(actions) == 1
    assert actions[0].status == SYNC_ACTION_ANOMALY
    assert actions[0].anomaly_category == SYNC_ANOMALY_UNKNOWN_PLAYER
    assert actions[0].player_id is None


async def test_sync_guild_multiple_tier_roles_never_writes_mirror(
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
    members = [MemberView(id=1111, role_ids=(seeded["role_id"], 7778))]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members
    )

    assert outcome.observations_applied == 0
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert any(
        a.anomaly_category == "multiple_tier_roles"
        and a.status == SYNC_ACTION_ANOMALY
        for a in actions
    )
    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert mirror is None


async def test_sync_guild_missing_tier_is_report_only(session_factory, clean_db):
    seeded = await _seed(session_factory)
    members = [MemberView(id=1111, role_ids=())]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members
    )

    assert outcome.observations_applied == 0
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert any(
        a.anomaly_category == "missing_tier" and a.status == SYNC_ACTION_ANOMALY
        for a in actions
    )
    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert mirror is None


async def test_sync_guild_collects_unknown_roles(session_factory, clean_db):
    await _seed(session_factory)
    members = [MemberView(id=1111, role_ids=(99999,))]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members
    )

    assert outcome.observations_applied == 0
    assert outcome.unknown_roles == (99999,)
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert any(
        a.anomaly_category == "unknown_roles" and a.status == SYNC_ACTION_ANOMALY
        for a in actions
    )


async def test_sync_guild_all_members_failed_marks_run_failed(
    session_factory, clean_db
):
    class BrokenMember:
        @property
        def id(self):
            return 1111

        @property
        def role_ids(self):
            raise RuntimeError("boom")

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=[BrokenMember()]
    )

    assert outcome.status == SYNC_RUN_FAILED
    assert outcome.failed_members == 1
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert len(actions) == 1
    assert actions[0].status == SYNC_ACTION_FAILED
    assert "boom" in actions[0].details["error"]


async def test_sync_guild_mixed_failure_marks_run_partial(session_factory, clean_db):
    seeded = await _seed(session_factory)

    class BrokenMember:
        @property
        def id(self):
            return 1111

        @property
        def role_ids(self):
            raise RuntimeError("boom")

    members = [MemberView(id=1111, role_ids=(seeded["role_id"],)), BrokenMember()]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members
    )

    assert outcome.status == SYNC_RUN_PARTIAL
    assert outcome.failed_members == 1
    assert outcome.observations_applied == 1
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert len(actions) == 2
    assert {a.status for a in actions} == {SYNC_ACTION_APPLIED, SYNC_ACTION_FAILED}


async def test_sync_guild_empty_guild_succeeds(session_factory, clean_db):
    outcome = await DiscordSyncService().sync_guild(session_factory, members=[])
    assert outcome.status == SYNC_RUN_SUCCESS
    assert outcome.scanned_members == 0
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert run.summary["scanned"] == 0
    assert actions == []


async def test_sync_guild_changed_tier_is_recorded_and_mirrored(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            tier_id=seeded["tier"].id,
            discord_role_id=seeded["role_id"],
            observed_at=datetime.now(timezone.utc),
            source="discord_sync",
        )
    async with transaction(session_factory) as session:
        t1 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t1")
            )
        ).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=seeded["kit"].id, tier_id=t1.id, discord_role_id=7778
        )
    members = [MemberView(id=1111, role_ids=(7778,))]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members, observed_at=datetime.now(timezone.utc)
    )

    assert outcome.observations_applied == 1
    run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    applied = [a for a in actions if a.status == SYNC_ACTION_APPLIED]
    assert len(applied) == 1
    assert applied[0].details["tier_changed"] is True
    assert applied[0].tier_id == t1.id
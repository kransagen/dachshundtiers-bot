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


async def test_sync_guild_member_without_tier_roles_is_silent(session_factory, clean_db):
    await _seed(session_factory)
    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=[MemberView(id=1111, role_ids=())]
    )
    assert outcome.observations_applied == 0
    assert outcome.anomalies == 0
    _run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert actions == []


async def test_sync_guild_missing_on_discord_is_report_only(session_factory, clean_db):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            tier_id=seeded["tier"].id,
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="promotion",
        )

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=[MemberView(id=1111, role_ids=())]
    )

    assert outcome.observations_applied == 0
    _run, actions = await _run_summary(session_factory, outcome.sync_run_id)
    assert [(a.anomaly_category, a.status) for a in actions] == [
        ("missing_on_discord", SYNC_ACTION_ANOMALY)
    ]
    assert outcome.changes[0].old_tier == "t2"
    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert mirror is not None and mirror.tier_id == seeded["tier"].id


async def test_sync_guild_collects_unknown_roles(session_factory, clean_db):
    await _seed(session_factory)
    members = [MemberView(id=1111, role_ids=(99999,))]

    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=members
    )

    assert outcome.observations_applied == 0
    assert outcome.unknown_roles == (99999,)


async def test_sync_guild_reads_roles_of_real_discord_members(session_factory, clean_db):
    """discord.Member nemá ``role_ids`` – role se čtou z ``member.roles``."""
    from types import SimpleNamespace

    seeded = await _seed(session_factory)
    member = SimpleNamespace(
        id=1111, display_name="Mirror", roles=[SimpleNamespace(id=seeded["role_id"])]
    )
    outcome = await DiscordSyncService().sync_guild(session_factory, members=[member])
    assert outcome.failed_members == 0
    assert outcome.tier_changes == 1


async def test_sync_guild_unchanged_reobservation_writes_no_action(session_factory, clean_db):
    seeded = await _seed(session_factory)
    members = [MemberView(id=1111, role_ids=(seeded["role_id"],))]
    await DiscordSyncService().sync_guild(session_factory, members=members)
    second = await DiscordSyncService().sync_guild(session_factory, members=members)
    assert second.tier_changes == 0
    _run, actions = await _run_summary(session_factory, second.sync_run_id)
    assert actions == []


async def test_sync_guild_creates_missing_player_when_enabled(session_factory, clean_db):
    from types import SimpleNamespace

    seeded = await _seed(session_factory)
    member = SimpleNamespace(
        id=2222, display_name="NewGuy", roles=[SimpleNamespace(id=seeded["role_id"])]
    )
    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=[member], create_missing_players=True
    )
    assert outcome.created_players == 1
    assert outcome.tier_changes == 1
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 2222)
        assert player is not None and player.ign == "NewGuy"
        mirror = await MirrorRepository().get_current(
            session, player_id=player.id, kit_id=seeded["kit"].id
        )
    assert mirror.tier_id == seeded["tier"].id


async def test_sync_guild_dry_run_writes_nothing(session_factory, clean_db):
    from types import SimpleNamespace

    from db.models import SyncRun

    seeded = await _seed(session_factory)
    member = SimpleNamespace(
        id=2222, display_name="NewGuy", roles=[SimpleNamespace(id=seeded["role_id"])]
    )
    outcome = await DiscordSyncService().sync_guild(
        session_factory, members=[member], create_missing_players=True, dry_run=True
    )
    assert outcome.dry_run is True and outcome.sync_run_id is None
    assert [c.kind for c in outcome.changes] == ["player_created", "tier_added"]
    async with transaction(session_factory) as session:
        assert await PlayerRepository().get_by_discord_id(session, 2222) is None
        assert (await session.execute(select(SyncRun))).scalars().all() == []


async def test_sync_guild_db_error_in_one_member_does_not_abort_others(
    session_factory, clean_db
):
    """Savepoint na člena: DB chyba u jednoho člena nerozbije transakci ostatních."""
    from types import SimpleNamespace

    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await PlayerRepository().claim_discord_id(session, discord_id=3333, ign="Other")

    service = DiscordSyncService()
    original = service._mirror_service.apply_observations

    async def flaky(session, *, player_id, **kwargs):
        if player_id == seeded["player"].id:
            from sqlalchemy import text

            await session.execute(text("SELECT 1/0"))
        return await original(session, player_id=player_id, **kwargs)

    service._mirror_service.apply_observations = flaky
    members = [
        SimpleNamespace(id=1111, display_name="Mirror", roles=[SimpleNamespace(id=seeded["role_id"])]),
        SimpleNamespace(id=3333, display_name="Other", roles=[SimpleNamespace(id=seeded["role_id"])]),
    ]
    outcome = await service.sync_guild(session_factory, members=members)
    assert outcome.failed_members == 1
    assert outcome.tier_changes == 1
    assert outcome.status == SYNC_RUN_PARTIAL


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
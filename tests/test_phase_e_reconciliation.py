"""Phase E, E3 — reconciliation contract tests.

Proves periodic reconciliation is STRICTLY observe-only on Discord:
draining the outbox and mirroring guild roles into PostgreSQL performs
zero Discord role mutations (add_roles / remove_roles / edit_role /
set_roles are never awaited), and a stale mirror converges to the
Discord-observed state (Discord wins, never vice versa).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import select

from db.models import Kit, TierDefinition
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.sync_audit import SyncRunRepository
from db.repositories.tiers import MirrorRepository, MirrorServiceRepository
from db.services.promotion import enqueue_promotion_wedge
from db.services.reconciliation import ReconciliationService
from db.services.session import transaction

from tests.test_services_outbox_consumer import _valid_payload


@dataclass
class SpyMember:
    id: int
    role_ids: tuple[int, ...]
    add_roles: mock.AsyncMock = field(default_factory=mock.AsyncMock)
    remove_roles: mock.AsyncMock = field(default_factory=mock.AsyncMock)
    edit_role: mock.AsyncMock = field(default_factory=mock.AsyncMock)
    set_roles: mock.AsyncMock = field(default_factory=mock.AsyncMock)


MUTATION_API = ("add_roles", "remove_roles", "edit_role", "set_roles")


async def _seed(session_factory, *, t2_role_id=7777, t1_role_id=7778):
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t1 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t1")
            )
        ).scalar_one()
        t2 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t2")
            )
        ).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t2.id, discord_role_id=t2_role_id
        )
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t1.id, discord_role_id=t1_role_id
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Reconcile"
        )
        _, evaluator = await PlayerRepository().claim_discord_id(
            session, discord_id=2222, ign="ReconcileTester"
        )
        return {
            "kit": kit,
            "t1": t1,
            "t2": t2,
            "tier": t2,
            "player": player,
            "evaluator": evaluator,
            "t2_role_id": t2_role_id,
            "t1_role_id": t1_role_id,
        }


async def test_reconciliation_never_mutates_discord_and_mirror_follows(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            tier_id=seeded["t1"].id,
            discord_role_id=seeded["t1_role_id"],
            observed_at=datetime.now(timezone.utc),
            source="promotion",
        )
    member = SpyMember(id=1111, role_ids=(seeded["t2_role_id"],))
    outcome = await ReconciliationService().reconcile(
        session_factory, members=[member]
    )

    for api in MUTATION_API:
        getattr(member, api).assert_not_awaited()

    assert outcome.sync.scanned_members == 1
    assert outcome.sync.observations_applied == 1
    assert outcome.sync.anomalies == 0

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        runs = await SyncRunRepository().list(session, limit=3)
    assert mirror is not None
    assert mirror.tier_id == seeded["t2"].id
    assert mirror.discord_role_id == seeded["t2_role_id"]
    assert mirror.source == "discord_sync"
    assert any(r.command == "reconcile" and r.mode == "automatic" for r in runs)


async def test_reconciliation_drains_confirmed_wedge(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="e3-reconcile",
        payload=_valid_payload(seeded, result_key="e3-reconcile"),
        discord_role_confirmed=True,
    )
    member = SpyMember(id=1111, role_ids=(seeded["t2_role_id"],))
    outcome = await ReconciliationService().reconcile(
        session_factory, members=[member]
    )

    assert len(outcome.outbox_consumed) == 1
    for api in MUTATION_API:
        getattr(member, api).assert_not_awaited()

    async with transaction(session_factory) as session:
        from db.models import Result
        from db.repositories.outbox import OUTBOX_DONE, OutboxRepository

        result = (
            await session.execute(
                select(Result).where(Result.result_key == "e3-reconcile")
            )
        ).scalar_one()
        done = await OutboxRepository().list_by_status(session, status=OUTBOX_DONE)
    assert result.promotion_status == "committed"
    assert len(done) == 1


async def test_reconcile_member_single_observation(session_factory, clean_db):
    seeded = await _seed(session_factory)
    member = SpyMember(id=1111, role_ids=(seeded["t2_role_id"],))
    outcome = await ReconciliationService().observe_member(
        session_factory, member=member
    )

    for api in MUTATION_API:
        getattr(member, api).assert_not_awaited()
    assert outcome.scanned_members == 1
    assert outcome.observations_applied == 1

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert mirror is not None and mirror.tier_id == seeded["t2"].id


async def test_reconciliation_is_idempotent(session_factory, clean_db):
    seeded = await _seed(session_factory)
    member = SpyMember(id=1111, role_ids=(seeded["t2_role_id"],))
    service = ReconciliationService()

    first = await service.reconcile(session_factory, members=[member])
    second = await service.reconcile(
        session_factory,
        members=[member],
        observed_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    assert first.sync.observations_applied == 1
    for api in MUTATION_API:
        getattr(member, api).assert_not_awaited()

    async with transaction(session_factory) as session:
        from db.models import TierHistory

        history = (
            await session.execute(
                select(TierHistory).where(
                    TierHistory.player_id == seeded["player"].id
                )
            )
        ).scalars().all()
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert second.sync.status == "success"
    assert len(history) == 1
    assert mirror is not None and mirror.tier_id == seeded["t2"].id
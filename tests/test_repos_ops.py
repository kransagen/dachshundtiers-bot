"""Results, tickets, cooldowns, queues repository tests."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db.models import Kit
from db.repositories.cooldowns import CooldownRepository
from db.repositories.kits import ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.queues import (
    QUEUE_ENTRY_PULLED,
    QueueEntryRepository,
    QueueRepository,
)
from db.repositories.results import (
    PROMOTION_COMMITTED,
    PROMOTION_DISCORD_PENDING,
    ResultRepository,
)
from db.repositories.tickets import TicketMemberRepository, TicketRepository
from db.services.session import transaction


def _player_repo() -> PlayerRepository:
    return PlayerRepository()


async def _seed_player(session, repo, discord_id: int, ign: str):
    _, player = await repo.claim_discord_id(session, discord_id=discord_id, ign=ign)
    return player


async def _seed_dimensions(session) -> Kit:
    await ensure_dimensions(
        session,
        (("ht3", "HT3"),),
        (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
    )
    return (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()


async def test_result_insert_and_key_uniqueness(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        player = await _seed_player(session, _player_repo(), 1, "Res1")
        row = await ResultRepository().insert(
            session,
            result_key="r-key-1",
            kind="ticket",
            player_id=player.id,
            kit_id=kit.id,
            promotion_status=PROMOTION_DISCORD_PENDING,
            new_tier_id=1,
        )
        assert row.promotion_status == PROMOTION_DISCORD_PENDING
    with pytest.raises(Exception):
        async with transaction(session_factory) as session:
            await ResultRepository().insert(
                session,
                result_key="r-key-1",
                kind="ticket",
                player_id=1,
                kit_id=1,
            )


async def test_result_promotion_lifecycle(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        player = await _seed_player(session, _player_repo(), 2, "Res2")
        repo = ResultRepository()
        await repo.insert(
            session,
            result_key="r-promo-1",
            kind="ticket",
            player_id=player.id,
            kit_id=kit.id,
            promotion_status=PROMOTION_DISCORD_PENDING,
        )
    async with transaction(session_factory) as session:
        repo = ResultRepository()
        pending = await repo.list_pending_commits(session)
        assert len(pending) == 1
        await repo.set_promotion_status(
            session, result_key="r-promo-1", promotion_status=PROMOTION_COMMITTED
        )
    async with transaction(session_factory) as session:
        row = await ResultRepository().get_by_key(session, "r-promo-1")
    assert row is not None
    assert row.promotion_status == PROMOTION_COMMITTED
    async with transaction(session_factory) as session:
        listed = await ResultRepository().list_for_player(
            session, player_id=row.player_id
        )
    assert [r.result_key for r in listed] == ["r-promo-1"]


async def test_cooldown_waitlist_upsert_single_row(session_factory, clean_db):
    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        player = await _seed_player(session, _player_repo(), 10, "Cool1")
        repo = CooldownRepository()
        first = await repo.upsert(
            session, player_id=player.id, cooldown_type="waitlist",
            expires_at=now + timedelta(hours=1)
        )
        second = await repo.upsert(
            session, player_id=player.id, cooldown_type="waitlist",
            expires_at=now + timedelta(hours=2)
        )
        assert first.id == second.id
        active = await repo.get_active(session, player_id=player.id, now=now)
        assert len(active) == 1
        assert active[0].expires_at > now + timedelta(minutes=90)


async def test_cooldown_kit_upsert_and_waitlist_coexist(session_factory, clean_db):
    now = datetime.now(timezone.utc)
    async with transaction(session_factory) as session:
        player = await _seed_player(session, _player_repo(), 11, "Cool2")
        kit = await _seed_dimensions(session)
        repo = CooldownRepository()
        await repo.upsert(session, player_id=player.id, cooldown_type="waitlist",
                          expires_at=now + timedelta(hours=1))
        await repo.upsert(session, player_id=player.id, cooldown_type="ht3",
                          kit_id=kit.id, expires_at=now + timedelta(hours=3))
        active = await repo.get_active(session, player_id=player.id, now=now)
        assert len(active) == 2
        is_wait = await repo.is_active(session, player_id=player.id,
                                       cooldown_type="waitlist", now=now)
        expired = await repo.is_active(session, player_id=player.id,
                                       cooldown_type="waitlist",
                                       now=now + timedelta(hours=10))
    assert is_wait is True
    assert expired is False


async def test_ticket_open_unique_and_close(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        player = await _seed_player(session, _player_repo(), 20, "Tick1")
        repo = TicketRepository()
        await repo.open(
            session, channel_id=111, player_id=player.id, ign="Tick1",
            kit_id=kit.id, ticket_type="eval"
        )
    with pytest.raises(Exception):
        async with transaction(session_factory) as session:
            player = await _seed_player(session, _player_repo(), 20, "Tick1")
            await TicketRepository().open(
                session, channel_id=222, player_id=player.id, ign="Tick1",
                kit_id=kit.id, ticket_type="eval"
            )
    async with transaction(session_factory) as session:
        repo = TicketRepository()
        assert len(await repo.list_open(session)) == 1
        closed = await repo.close_by_channel(session, channel_id=111)
        assert closed.status == "closed"
        assert len(await repo.list_open(session)) == 0


async def test_ticket_members_add_remove(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        owner = await _seed_player(session, _player_repo(), 21, "Tick2")
        member = await _seed_player(session, _player_repo(), 22, "Tick3")
        ticket = await TicketRepository().open(
            session, channel_id=333, player_id=owner.id, ign="Tick2",
            kit_id=kit.id
        )
        members = TicketMemberRepository()
        await members.add(session, ticket_id=ticket.id, player_id=member.id)
        assert await members.has_member(session, ticket_id=ticket.id, player_id=member.id)
        removed = await members.remove(session, ticket_id=ticket.id, player_id=member.id)
        assert removed is True
        assert not await members.has_member(session, ticket_id=ticket.id, player_id=member.id)


async def test_queue_flow_and_positions(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        q = await QueueRepository().open(session, kit_id=kit.id, name="Q1")
        entries = QueueEntryRepository()
        p1 = await _seed_player(session, _player_repo(), 30, "QP1")
        p2 = await _seed_player(session, _player_repo(), 31, "QP2")
        e1 = await entries.enqueue(session, queue_id=q.id, player_id=p1.id,
                                   ign="QP1", kit_id=kit.id)
        e2 = await entries.enqueue(session, queue_id=q.id, player_id=p2.id,
                                   ign="QP2", kit_id=kit.id)
        assert e1.position == 1 and e2.position == 2
        assert await entries.waiting_count(session, queue_id=q.id) == 2
        pulled = await entries.transition(
            session, entry_id=e1.id, status=QUEUE_ENTRY_PULLED
        )
        assert pulled.status == QUEUE_ENTRY_PULLED
        assert await entries.waiting_count(session, queue_id=q.id) == 1
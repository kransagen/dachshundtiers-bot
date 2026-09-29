"""DB (PostgreSQL) režim nových front-životního-cyklu služeb — Phase F (todo #10b).

Pokrývá open/close fronty, queue-scoped testery (opener + /joinasqueue),
/queue list snapshot, /pull peek, /removeq a /skip v produkční DB cestě.
JSON režim (session_factory=None) je pokryt test_queue_lifecycle_json.py.
"""


from sqlalchemy import select

from db.models import QueueEntry, QueueTester
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.queues import QUEUE_ENTRY_LEFT, QueueRepository
from db.services.session import transaction
from services import queue_service as qsvc

KITS = (("anchorpvp", "AnchorPvP"), ("molepvp", "MolePVP"))
TIERS = (("LT5", "ladder", "LT5", 1),)


async def _seed(session_factory, *, open_kits=()):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        kit_repo = KitRepository()
        queue_repo = QueueRepository()
        for key in open_kits:
            kit = await kit_repo.get_by_key(session, key)
            await queue_repo.open(session, kit_id=kit.id, name=kit.name)
        await session.flush()


async def _count(session_factory, model):
    async with transaction(session_factory) as session:
        result = await session.execute(select(model.id))
        return len(list(result.scalars()))


async def test_open_queue_ok(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)

    status, qdata = await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )
    assert status == "ok"
    assert qdata["name"] == "AnchorPvP"
    assert qdata["opener"] == "111"
    assert qdata["testers"] == ["111"]
    assert isinstance(qdata["time"], int)

    async with transaction(session_factory) as session:
        row = (
            await session.execute(select(QueueTester).limit(1))
        ).scalar_one()
        assert row.player_id is not None

    second_status, _qdata = await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "222", "Second", session_factory=session_factory
    )
    assert second_status == "exists"


async def test_open_queue_unknown_kit(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
    status, qdata = await qsvc.open_queue(
        "nope", "Nope", "111", "Opener", session_factory=session_factory
    )
    assert status == "unknown_kit"
    assert qdata is None


async def test_queue_state_and_close(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )
    qdata = await qsvc.queue_state("anchorpvp", session_factory=session_factory)
    assert qdata is not None and qdata["opener"] == "111"

    old = await qsvc.close_queue("anchorpvp", session_factory=session_factory)
    assert old is None
    assert await qsvc.queue_state("anchorpvp", session_factory=session_factory) is None

    async with transaction(session_factory) as session:
        active = await QueueRepository().list_active(session)
        assert active == []


async def test_join_and_leave_queue_tester(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )

    status, qdata = await qsvc.join_queue_tester(
        "anchorpvp", "222", "Second", session_factory=session_factory
    )
    assert status == "ok"
    assert set(qdata["testers"]) == {"111", "222"}

    dup_status, _ = await qsvc.join_queue_tester(
        "anchorpvp", "222", "Second", session_factory=session_factory
    )
    assert dup_status == "duplicate"

    closed_status, _ = await qsvc.join_queue_tester(
        "molepvp", "333", "Third", session_factory=session_factory
    )
    assert closed_status == "closed"

    status, qdata = await qsvc.leave_queue_tester(
        "anchorpvp", "111", session_factory=session_factory
    )
    assert status == "ok"
    assert qdata["opener"] == "222"
    assert qdata["testers"] == ["222"]

    not_listed_status, _ = await qsvc.leave_queue_tester(
        "anchorpvp", "111", session_factory=session_factory
    )
    assert not_listed_status == "not_listed"

    async with transaction(session_factory) as session:
        rows = await session.execute(select(QueueTester))
        assert len(list(rows.scalars())) == 1


async def test_register_global_tester_db(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        assert await qsvc.register_global_tester(
            "777", "Seven", session_factory=session_factory
        )
        assert await qsvc.register_global_tester(
            "777", "Seven", session_factory=session_factory
        )
        player = await PlayerRepository().get_by_discord_id(session, 777)
        assert player is not None


async def test_list_entries_and_snapshot(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )
    await qsvc.join_queue("1", "alice", "AliceMC", "AnchorPvP",
                          joined_at_ms=1_700_000_000_000,
                          cooldown_ms=4 * 24 * 60 * 60 * 1000,
                          session_factory=session_factory)
    await qsvc.join_queue("2", "bob", "BobMC", "AnchorPvP",
                          joined_at_ms=1_700_000_000_100,
                          cooldown_ms=4 * 24 * 60 * 60 * 1000,
                          session_factory=session_factory)

    entries = await qsvc.list_queue_entries(
        "anchorpvp", session_factory=session_factory
    )
    assert [e["id"] for e in entries] == ["1", "2"]
    assert entries[0]["ign"] == "AliceMC"

    snapshot_entries, active = await qsvc.queue_snapshot(
        session_factory=session_factory
    )
    assert [e["id"] for e in snapshot_entries] == ["1", "2"]
    assert "anchorpvp" in active

    first = await qsvc.peek_first_player(session_factory=session_factory)
    assert first["id"] == "1"


async def test_removeq_db(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )
    await qsvc.join_queue("1", "alice", "AliceMC", "AnchorPvP",
                          joined_at_ms=1_700_000_000_000,
                          cooldown_ms=4 * 24 * 60 * 60 * 1000,
                          session_factory=session_factory)

    entry = await qsvc.removeq("1", session_factory=session_factory)
    assert entry is not None and entry["id"] == "1"
    assert await qsvc.removeq("1", session_factory=session_factory) is None

    async with transaction(session_factory) as session:
        status = (
            await session.execute(
                select(QueueEntry.status).where(QueueEntry.player_id.is_not(None))
            )
        ).scalars().all()
    assert status == [QUEUE_ENTRY_LEFT]


async def _join_two(session_factory):
    await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )
    for uid, name, ign, ts in (("1", "alice", "AliceMC", 0), ("2", "bob", "BobMC", 100)):
        await qsvc.join_queue(uid, name, ign, "AnchorPvP",
                              joined_at_ms=1_700_000_000_000 + ts,
                              cooldown_ms=4 * 24 * 60 * 60 * 1000,
                              session_factory=session_factory)


async def _statuses(session_factory):
    async with transaction(session_factory) as session:
        rows = (
            await session.execute(
                select(QueueEntry.status, QueueEntry.removed_reason).order_by(QueueEntry.id)
            )
        ).all()
    return [tuple(r) for r in rows]


async def test_skip_pulled_player_removes_from_queue_db(session_factory, clean_db):
    await _seed(session_factory)
    await _join_two(session_factory)
    async with transaction(session_factory) as session:
        entry = (await session.execute(select(QueueEntry).order_by(QueueEntry.id))).scalars().first()
        entry.status = "pulled"
        entry.room_channel_id = 4242

    result = await qsvc.skip_player("1", session_factory=session_factory)
    assert result["removed"] is True
    assert result["was_pulled"] is True
    assert result["channel_id"] == 4242
    assert result["kit_key"] == "anchorpvp"
    assert result["next"]["id"] == "2"

    entries = await qsvc.list_queue_entries("anchorpvp", session_factory=session_factory)
    assert [e["id"] for e in entries] == ["2"]
    assert (await _statuses(session_factory))[0] == ("left", "skip")


async def test_skip_waiting_player_removes_from_queue_db(session_factory, clean_db):
    await _seed(session_factory)
    await _join_two(session_factory)

    result = await qsvc.skip_player("1", session_factory=session_factory)
    assert result["removed"] is True
    assert result["was_pulled"] is False
    assert result["next"]["id"] == "2"
    entries = await qsvc.list_queue_entries("anchorpvp", session_factory=session_factory)
    assert [e["id"] for e in entries] == ["2"]


async def test_skip_unknown_player_is_noop_db(session_factory, clean_db):
    await _seed(session_factory)
    await _join_two(session_factory)
    result = await qsvc.skip_player("999", session_factory=session_factory)
    assert result["removed"] is False
    assert await _statuses(session_factory) == [("waiting", None), ("waiting", None)]


async def test_set_queue_panel_and_message_id(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.open_queue(
        "anchorpvp", "AnchorPvP", "111", "Opener", session_factory=session_factory
    )
    await qsvc.set_queue_panel("anchorpvp", 555_555, 999_999,
                               session_factory=session_factory)
    mid = await qsvc.panel_message_id("anchorpvp", session_factory=session_factory)
    assert mid == "999999"

    async with transaction(session_factory) as session:
        queue = await QueueRepository().get_active(
            session, kit_id=(await KitRepository().get_by_name(session, "anchorpvp")).id
        )
        assert queue.panel_message_id == 999_999
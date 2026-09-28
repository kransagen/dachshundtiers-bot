"""DB (PostgreSQL) režim services/queue_service.py — Phase F (todo #8).

JSON režim (session_factory=None) je pokryt test_queue_service.py; tento soubor
ověřuje, že produkční cesta přes ``session_factory`` zachovává JSON kontrakt
(výsledky join/leave/pop/remove, cooldown, duplicita) nad Queue/QueueEntry
řádky a že vytažený hráč má ``username`` + ``room_channel_id``.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from db.models import Player, QueueEntry
from db.repositories.cooldowns import COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.queues import (
    QUEUE_ENTRY_LEFT,
    QUEUE_ENTRY_PULLED,
    QUEUE_ENTRY_TESTED,
    QUEUE_ENTRY_WAITING,
    QueueRepository,
)
from db.services.session import transaction
from services import queue_service as qsvc

NOW_MS = 1_700_000_000_000
COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000

KITS = (("anchorpvp", "AnchorPvP"), ("molepvp", "MolePVP"))
TIERS = (("LT5", "ladder", "LT5", 1),)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def _seed(session_factory, *, open_kits=("anchorpvp",)):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        kit_repo = KitRepository()
        queue_repo = QueueRepository()
        for key in open_kits:
            kit = await kit_repo.get_by_key(session, key)
            await queue_repo.open(session, kit_id=kit.id, name=kit.name)
        await session.flush()


async def _join(session_factory, uid, name="alice", ign="AliceMC", kit="AnchorPvP", at=None):
    return await qsvc.join_queue(
        uid,
        name,
        ign,
        kit,
        joined_at_ms=at if at is not None else NOW_MS,
        cooldown_ms=COOLDOWN_MS,
        session_factory=session_factory,
    )


async def _entries(session_factory) -> list[QueueEntry]:
    async with transaction(session_factory) as session:
        result = await session.execute(
            select(QueueEntry).order_by(QueueEntry.position)
        )
        return list(result.scalars())


async def test_join_success(session_factory, clean_db):
    await _seed(session_factory)
    result = await _join(session_factory, "1")
    assert result["result"] == "joined"

    entries = await _entries(session_factory)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.status == QUEUE_ENTRY_WAITING
    assert entry.position == 1
    assert entry.ign == "AliceMC"
    assert entry.username == "alice"


async def test_join_closed_queue(session_factory, clean_db):
    await _seed(session_factory, open_kits=("anchorpvp",))
    result = await _join(session_factory, "1", kit="MolePVP")
    assert result["result"] == "closed"
    assert await _entries(session_factory) == []

    result = await _join(session_factory, "1", kit="Nope")
    assert result["result"] == "closed"


async def test_join_cooldown_blocks(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, "1")
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 1)
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=None,
            expires_at=_dt(NOW_MS + 60_000),
        )
    result = await _join(session_factory, "1", at=NOW_MS + 1_000)
    assert result["result"] == "cooldown"
    assert result["remaining"] > 0
    assert len(await _entries(session_factory)) == 1


async def test_join_after_cooldown_passes(session_factory, clean_db):
    await _seed(session_factory)
    async with transaction(session_factory) as session:
        _outcome, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1, ign="AliceMC"
        )
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=None,
            expires_at=_dt(NOW_MS - 1),
        )
    result = await _join(session_factory, "1")
    assert result["result"] == "joined"


async def test_join_cooldown_on_one_kit_never_blocks_another_kit(
    session_factory, clean_db
):
    """Business rule: cooldowns are per player+kit+type. A per-kit waitlist
    cooldown on AnchorPvP must not block the same player joining MolePVP."""
    await _seed(session_factory, open_kits=("anchorpvp", "molepvp"))
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 1)
        if player is None:
            _outcome, player = await PlayerRepository().claim_discord_id(
                session, discord_id=1, ign="AliceMC"
            )
        anchorpvp = await KitRepository().get_by_key(session, "anchorpvp")
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=anchorpvp.id,
            expires_at=_dt(NOW_MS + 60_000),
        )
    blocked = await _join(session_factory, "1", kit="AnchorPvP", at=NOW_MS + 1_000)
    assert blocked["result"] == "cooldown"

    allowed = await _join(session_factory, "1", kit="MolePVP", at=NOW_MS + 1_000)
    assert allowed["result"] == "joined"


async def test_join_duplicate_blocked(session_factory, clean_db):
    await _seed(session_factory)
    assert (await _join(session_factory, "1"))["result"] == "joined"
    result = await _join(session_factory, "1")
    assert result["result"] == "duplicate"
    assert len(await _entries(session_factory)) == 1


async def test_concurrent_joins_same_user_no_duplicate(session_factory, clean_db):
    await _seed(session_factory)
    r1, r2 = await asyncio.gather(
        _join(session_factory, "1"), _join(session_factory, "1")
    )
    assert sorted([r1["result"], r2["result"]]) == ["duplicate", "joined"]
    assert len(await _entries(session_factory)) == 1


async def test_concurrent_joins_two_users_both_land(session_factory, clean_db):
    await _seed(session_factory)
    r1, r2 = await asyncio.gather(
        _join(session_factory, "1", name="a", ign="A"),
        _join(session_factory, "2", name="b", ign="B"),
    )
    assert sorted([r1["result"], r2["result"]]) == ["joined", "joined"]
    entries = await _entries(session_factory)
    assert len(entries) == 2
    # E4: concurrent joins must never share a position (FIFO order).
    assert sorted(e.position for e in entries) == [1, 2]


async def test_concurrent_joins_many_users_distinct_positions(session_factory, clean_db):
    """E4 regression: N concurrent joins in the same queue get positions
    {1..N} — a plain MAX(position)+1 read would let two transactions observe
    the same MAX and insert the same position, silently corrupting FIFO
    order. Serialization on the queue row must hold regardless of how many
    joins overlap."""
    await _seed(session_factory)
    outcomes = await asyncio.gather(
        *[
            _join(session_factory, str(uid), name=f"u{uid}", ign=f"IGN{uid}")
            for uid in range(1, 7)
        ]
    )
    assert [o["result"] for o in outcomes] == ["joined"] * 6
    entries = await _entries(session_factory)
    assert sorted(e.position for e in entries) == list(range(1, 7))
    assert len({e.position for e in entries if e.status == QUEUE_ENTRY_WAITING}) == 6

    # FIFO pull order follows ascending positions (assignment of WHICH join
    # gets WHICH position is arbitrary under concurrency — only the ORDER
    # must be the position order).
    entries = await _entries(session_factory)
    async with transaction(session_factory) as session:
        players = await session.execute(
            select(Player).where(Player.id.in_([e.player_id for e in entries]))
        )
        discord_by_player = {
            p.id: p.discord_id for p in players.scalars()
        }
    position_by_discord = {
        discord_by_player[e.player_id]: e.position for e in entries
    }
    pulled_ids = []
    while True:
        player = await qsvc.pop_for_kit("anchorpvp", session_factory=session_factory)
        if player is None:
            break
        pulled_ids.append(player["id"])
    pulled_positions = [position_by_discord[int(uid)] for uid in pulled_ids]
    assert pulled_positions == sorted(position_by_discord.values())


async def test_leave_queue(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, "1")
    await _join(session_factory, "2", name="bob", ign="BobMC")
    assert await qsvc.leave_queue("1", "anchorpvp", session_factory=session_factory) is True

    entries = await _entries(session_factory)
    assert len(entries) == 2
    left = next(e for e in entries if e.position == 1)
    assert left.status == QUEUE_ENTRY_LEFT
    assert left.removed_reason == "leave"
    waiting = next(e for e in entries if e.position == 2)
    assert waiting.status == QUEUE_ENTRY_WAITING

    assert (
        await qsvc.leave_queue("1", "anchorpvp", session_factory=session_factory)
        is False
    )
    assert await qsvc.leave_queue("1", "nope", session_factory=session_factory) is False


async def test_pop_for_kit_pops_first_of_kit(session_factory, clean_db):
    await _seed(session_factory, open_kits=("anchorpvp", "molepvp"))
    await _join(session_factory, "1")
    await _join(session_factory, "2", name="bob", ign="BobMC", kit="MolePVP")
    await _join(session_factory, "3", name="carol", ign="CarolMC")

    first = await qsvc.pop_for_kit("anchorpvp", session_factory=session_factory)
    assert first is not None
    assert first["id"] == "1"
    assert first["kit"] == "AnchorPvP"
    assert first["username"] == "alice"
    assert first["joinedAt"] == NOW_MS

    entries = await _entries(session_factory)
    assert next(e for e in entries if e.ign == "AliceMC").status == QUEUE_ENTRY_PULLED

    second = await qsvc.pop_for_kit("anchorpvp", session_factory=session_factory)
    assert second["id"] == "3"
    assert await qsvc.pop_for_kit("anchorpvp", session_factory=session_factory) is None


async def test_remove_by_player_id(session_factory, clean_db):
    await _seed(session_factory, open_kits=("anchorpvp", "molepvp"))
    await _join(session_factory, "1")
    await _join(session_factory, "2", name="bob", ign="BobMC", kit="MolePVP")

    assert await qsvc.remove_by_player_id("1", session_factory=session_factory) is True
    entries = await _entries(session_factory)
    assert next(e for e in entries if e.ign == "AliceMC").status == QUEUE_ENTRY_PULLED
    assert next(e for e in entries if e.ign == "BobMC").status == QUEUE_ENTRY_WAITING
    assert await qsvc.remove_by_player_id("1", session_factory=session_factory) is False


async def test_pulled_player_roundtrip(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, "1")
    player = await qsvc.pop_for_kit("anchorpvp", session_factory=session_factory)
    assert player is not None

    await qsvc.save_pulled_player(player, 123456, session_factory=session_factory)
    entries = await _entries(session_factory)
    assert entries[0].status == QUEUE_ENTRY_PULLED
    assert entries[0].room_channel_id == 123456

    assert (
        await qsvc.remove_pulled_player("1", session_factory=session_factory) is True
    )
    entries = await _entries(session_factory)
    assert entries[0].status == QUEUE_ENTRY_TESTED
    assert entries[0].removed_reason == "tested"
    assert (
        await qsvc.remove_pulled_player("1", session_factory=session_factory) is False
    )


async def test_save_pulled_player_without_entry_noop(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.save_pulled_player(
        {"id": "99", "username": "", "ign": "Nobody", "kit": "", "joinedAt": 0},
        555,
        session_factory=session_factory,
    )
    assert await _entries(session_factory) == []


async def test_preset_player_room_on_waiting_entry(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, "1")
    await qsvc.preset_player_room(
        {"id": "1", "username": "alice", "ign": "AliceMC", "kit": "", "joinedAt": 0},
        777,
        session_factory=session_factory,
    )
    entries = await _entries(session_factory)
    assert len(entries) == 1
    assert entries[0].status == QUEUE_ENTRY_WAITING
    assert entries[0].room_channel_id == 777
    assert entries[0].pulled_at is None


async def test_preset_player_room_without_entry_noop(session_factory, clean_db):
    await _seed(session_factory)
    await qsvc.preset_player_room(
        {"id": "99", "username": "", "ign": "Nobody", "kit": "", "joinedAt": 0},
        777,
        session_factory=session_factory,
    )
    assert await _entries(session_factory) == []

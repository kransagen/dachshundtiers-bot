"""Regrese oprav auditu: fronty, tester roomky, evaly, tickety a views."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import discord
from sqlalchemy import select

from db.models import Player, QueueEntry
from db.repositories.cooldowns import COOLDOWN_HT3, CooldownRepository
from db.repositories.kits import KitRepository, KitTesterRoomRepository, ensure_dimensions
from db.repositories.queues import (
    QUEUE_ENTRY_WAITING,
    QueueRepository,
)
from db.services.session import transaction
from services import evals
from services import queue_service as qsvc
from services.ht3_tickets import ensure_eval_ticket

NOW_MS = 1_700_000_000_000
COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000
KITS = (("anchorpvp", "AnchorPvP"), ("molepvp", "MolePVP"))
TIERS = (("LT5", "ladder", "LT5", 1),)


async def _seed(session_factory, *, open_kits=("anchorpvp",)):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        for key in open_kits:
            kit = await KitRepository().get_by_key(session, key)
            await QueueRepository().open(session, kit_id=kit.id, name=kit.name)


async def _join(session_factory, uid, ign, kit="AnchorPvP", at=NOW_MS):
    from services.player_link import link_ign

    await link_ign(int(uid), ign, session_factory=session_factory)
    return await qsvc.join_queue(
        str(uid), ign.lower(), kit,
        joined_at_ms=at, cooldown_ms=COOLDOWN_MS, session_factory=session_factory,
    )


async def _entries(session_factory):
    async with transaction(session_factory) as session:
        rows = await session.execute(select(QueueEntry).order_by(QueueEntry.id))
        return list(rows.scalars())


async def test_requeue_pulled_player_keeps_position(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, 1, "AliceMC", at=NOW_MS)
    await _join(session_factory, 2, "BobMC", at=NOW_MS + 1)
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "anchorpvp")
        await KitTesterRoomRepository().set_room(session, kit_id=kit.id, channel_id=555)

    pulled = await qsvc.pull_for_kit("anchorpvp", session_factory=session_factory)
    assert pulled.player["id"] == "1"

    assert await qsvc.requeue_pulled_player(
        pulled.player, "AnchorPvP", session_factory=session_factory
    )
    entries = await _entries(session_factory)
    assert [e.status for e in entries] == [QUEUE_ENTRY_WAITING, QUEUE_ENTRY_WAITING]
    assert entries[0].pulled_at is None

    again = await qsvc.pull_for_kit("anchorpvp", session_factory=session_factory)
    assert again.player["id"] == "1"


async def test_requeue_skips_player_who_rejoined(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, 1, "AliceMC")
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "anchorpvp")
        await KitTesterRoomRepository().set_room(session, kit_id=kit.id, channel_id=555)
    pulled = await qsvc.pull_for_kit("anchorpvp", session_factory=session_factory)
    await _join(session_factory, 1, "AliceMC", at=NOW_MS + 5)

    assert not await qsvc.requeue_pulled_player(
        pulled.player, "AnchorPvP", session_factory=session_factory
    )
    statuses = sorted(e.status for e in await _entries(session_factory))
    assert statuses == ["left", QUEUE_ENTRY_WAITING]


async def test_tester_room_helpers(session_factory, clean_db):
    await _seed(session_factory)
    assert not await qsvc.is_tester_room(555, session_factory=session_factory)
    await qsvc.set_tester_room("anchorpvp", 555, session_factory=session_factory)
    assert await qsvc.is_tester_room(555, session_factory=session_factory)
    assert await qsvc.clear_tester_room_for_channel(555, session_factory=session_factory)
    assert await qsvc.resolve_tester_room("anchorpvp", session_factory=session_factory) is None
    assert not await qsvc.clear_tester_room_for_channel(555, session_factory=session_factory)


async def test_last_tester_cannot_leave(session_factory, clean_db):
    await _seed(session_factory, open_kits=())
    await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "O", session_factory=session_factory)
    status, qdata = await qsvc.leave_queue_tester(
        "anchorpvp", "111", session_factory=session_factory
    )
    assert status == "last_tester"
    assert qdata["testers"] == ["111"]


async def test_concurrent_double_clicks_do_not_raise(session_factory, clean_db):
    await _seed(session_factory, open_kits=())
    await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "O", session_factory=session_factory)
    results = await asyncio.gather(
        *[
            qsvc.join_queue_tester("anchorpvp", "222", session_factory=session_factory)
            for _ in range(4)
        ]
    )
    assert sorted(r[0] for r in results).count("ok") == 1

    registered = await asyncio.gather(
        *[qsvc.register_global_tester("333", "x", session_factory=session_factory) for _ in range(4)]
    )
    assert all(registered)

    async with transaction(session_factory) as session:
        from db.repositories.players import PlayerRepository

        await PlayerRepository().get_or_create_by_discord_id(session, discord_id=444, ign="Steve")
    granted = await asyncio.gather(
        *[evals.set_eval("Steve", "anchorpvp", granted_by=9, session_factory=session_factory) for _ in range(4)]
    )
    assert all(granted)


async def test_inactive_kit_cannot_open_join_or_pull(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, 1, "AliceMC")
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "anchorpvp")
        await KitTesterRoomRepository().set_room(session, kit_id=kit.id, channel_id=555)
        kit.active = False
        mole = await KitRepository().get_by_key(session, "molepvp")
        mole.active = False

    assert (await _join(session_factory, 2, "BobMC"))["result"] == "closed"
    assert (await qsvc.pull_for_kit("anchorpvp", session_factory=session_factory)).status == (
        qsvc.PULL_NO_KIT
    )
    status, _ = await qsvc.open_queue(
        "molepvp", "MolePVP", "111", "O", session_factory=session_factory
    )
    assert status == "unknown_kit"


async def test_entries_of_closed_queue_are_not_matched_by_player(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, 1, "AliceMC")
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "anchorpvp")
        queue = await QueueRepository().get_active(session, kit_id=kit.id)
        await QueueRepository().close(session, queue_id=queue.id)

    assert await qsvc.removeq("1", session_factory=session_factory) is None
    skipped = await qsvc.skip_player("1", session_factory=session_factory)
    assert skipped["removed"] is False


async def test_join_after_close_is_refused(session_factory, clean_db):
    await _seed(session_factory)
    await _join(session_factory, 1, "AliceMC")
    await qsvc.close_queue("anchorpvp", session_factory=session_factory)
    assert (await _join(session_factory, 2, "BobMC"))["result"] == "closed"
    statuses = [e.status for e in await _entries(session_factory)]
    assert statuses == ["left"]


async def _eval_player(session_factory, *, discord_id, ign="Steve"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        session.add(Player(ign=ign, discord_id=discord_id))


async def test_eval_ticket_without_discord_is_skipped(session_factory, clean_db):
    await _eval_player(session_factory, discord_id=None)
    request = await ensure_eval_ticket("Steve", "anchorpvp", session_factory=session_factory)
    assert request.needs_ticket is False
    assert request.reason == "no_discord"


async def test_eval_ticket_respects_ht3_cooldown(session_factory, clean_db):
    await _eval_player(session_factory, discord_id=900)
    async with transaction(session_factory) as session:
        player = (await session.execute(select(Player))).scalar_one()
        kit = await KitRepository().get_by_key(session, "anchorpvp")
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_HT3,
            kit_id=kit.id,
            expires_at=datetime.now(timezone.utc) + timedelta(days=2),
            source="ticket_close",
        )
    request = await ensure_eval_ticket("Steve", "anchorpvp", session_factory=session_factory)
    assert request.needs_ticket is False
    assert request.reason == "cooldown"
    assert request.remaining_ms > 0


async def test_set_eval_records_granter(session_factory, clean_db):
    await _eval_player(session_factory, discord_id=900)
    assert await evals.set_eval("Steve", "anchorpvp", granted_by=42, session_factory=session_factory)
    from db.models import Evaluation

    async with transaction(session_factory) as session:
        row = (await session.execute(select(Evaluation))).scalar_one()
    assert row.granted_by == 42


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------
def _interaction():
    inter = mock.MagicMock()
    inter.channel_id = 1001
    inter.user.id = 99
    inter.user.display_name = "tester-one"
    inter.client.db_session_factory = object()
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup.send = mock.AsyncMock()
    return inter


def test_pull_requeues_player_and_clears_mapping_when_room_deleted():
    import views

    inter = _interaction()
    inter.guild.get_channel.return_value = None
    inter.guild.fetch_channel = mock.AsyncMock(
        side_effect=discord.NotFound(mock.MagicMock(status=404), "gone")
    )
    player = {"id": "1", "kit": "AnchorPvP"}
    with mock.patch("views.requeue_pulled_player", new=mock.AsyncMock()) as requeue, \
         mock.patch("views.clear_tester_room_for_channel", new=mock.AsyncMock()) as clear, \
         mock.patch("views.save_pulled_player", new=mock.AsyncMock()) as save, \
         mock.patch("views.update_panel", new=mock.AsyncMock()):
        asyncio.run(views.grant_pull_access(inter, player, 555, "AnchorPvP", session_factory=object()))

    requeue.assert_awaited_once()
    clear.assert_awaited_once()
    save.assert_not_awaited()
    assert "neexistuje" in inter.followup.send.await_args.args[0]


def test_pull_keeps_mapping_on_transient_discord_error():
    import views

    inter = _interaction()
    inter.guild.get_channel.return_value = None
    inter.guild.fetch_channel = mock.AsyncMock(
        side_effect=discord.HTTPException(mock.MagicMock(status=500), "boom")
    )
    with mock.patch("views.requeue_pulled_player", new=mock.AsyncMock()) as requeue, \
         mock.patch("views.clear_tester_room_for_channel", new=mock.AsyncMock()) as clear, \
         mock.patch("views.update_panel", new=mock.AsyncMock()):
        asyncio.run(
            views.grant_pull_access(inter, {"id": "1"}, 555, "AnchorPvP", session_factory=object())
        )

    requeue.assert_awaited_once()
    clear.assert_not_awaited()


def test_unclaim_by_non_admin_is_not_forced():
    import views

    inter = _interaction()
    unclaim = mock.AsyncMock(
        return_value={"result": "not_claimer", "claimer_id": "5", "claimer_name": "Other"}
    )
    ticket = {"id": "1001", "status": "open"}
    with mock.patch("views.has_tester_role", return_value=True), \
         mock.patch("views.has_admin_role", return_value=False), \
         mock.patch("views.get_ticket", new=mock.AsyncMock(return_value=ticket)), \
         mock.patch("views.unclaim_ticket", new=unclaim):
        asyncio.run(views.HTTicketView().on_unclaim(inter))

    assert unclaim.await_args.kwargs["force"] is False
    assert "Other" in inter.followup.send.await_args.args[0]


def test_panel_select_is_capped_at_25_options():
    import views

    view = views.HT3PanelView(kits=[f"Kit{i}" for i in range(40)])
    assert len(view.children[0].options) == 25

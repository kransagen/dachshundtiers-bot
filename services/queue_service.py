"""Čistá logika front + transakční operace (JSON i PostgreSQL režim).

Veškeré změny probíhají atomicky: v JSON režimu přes ``services.store``
(kritický úsek čtení + kontrola + zápis), v DB režimu přes jednu PostgreSQL
transakci. Souběžné interakce nemůžou duplicitně zapsat hráče do fronty,
obejít cooldown, ztratit zápis (join vs. pull vs. leave) a vytažení (pull)
hráče už není „dvakrát".
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import IntegrityError

from db.models import Kit, Player, Queue, QueueEntry
from db.repositories.cooldowns import CooldownRepository
from db.repositories.kits import KitRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.queues import (
    QUEUE_ENTRY_LEFT,
    QUEUE_ENTRY_PULLED,
    QUEUE_ENTRY_TESTED,
    QueueEntryRepository,
    QueueRepository,
    QueueTesterRepository,
)
from db.repositories.evaluations import TesterRepository
from db.services.session import transaction as db_transaction
from services.store import transaction


def cooldown_remaining(cooldowns, user_id: str, now: int, cooldown_ms: int):
    """Zbývající milisekundy cooldownu hráče, nebo ``None``, když už vypršel."""
    if not isinstance(cooldowns, dict):
        return None
    last = cooldowns.get(user_id)
    if last is None:
        return None
    remaining = cooldown_ms - (now - last)
    return remaining if remaining > 0 else None


def already_in_queue(queue, user_id: str, kit_key: str) -> bool:
    """Je hráč (podle ID) už zapsaný ve frontě daného kitu?"""
    kit_key = str(kit_key).lower()
    uid = str(user_id)
    return any(
        p.get("id") == uid and str(p.get("kit", "")).lower() == kit_key
        for p in queue
    )


def move_to_queue_end(queue: list, stored_player, player_id: str):
    """Přesune hráče na konec fronty (ostatní jdou před něj).

    Vrací ``(new_queue, kit_key, moved)``. Pokud hráč ve frontě není,
    použije se uložený záznam z pullnutí (``stored_player``).
    """
    kit_key = ""
    entry_to_move = stored_player if (stored_player and stored_player.get("kit")) else None
    new_queue = []
    moved = False
    for p in queue:
        if str(p.get("id", "")) == player_id and not moved:
            entry_to_move = p
            moved = True
            continue
        new_queue.append(p)
    if entry_to_move and entry_to_move.get("kit"):
        new_queue.append(entry_to_move)
        kit_key = str(entry_to_move["kit"]).lower()
    return new_queue, kit_key, moved


def make_entry(user_id: str, username: str, ign: str, kit: str, joined_at_ms: int) -> dict:
    """Nový záznam hráče ve frontě (stejný tvar jako dřív)."""
    return {
        "id": str(user_id),
        "username": username,
        "ign": ign,
        "kit": kit,
        "joinedAt": joined_at_ms,
        "testerId": None,
    }


def _db_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _db_entry_to_dict(
    entry: QueueEntry, player: Optional[Player], kit_name: str
) -> dict:
    return {
        "id": str(player.discord_id or player.id) if player is not None else str(entry.player_id),
        "username": entry.username or "",
        "ign": entry.ign,
        "kit": kit_name,
        "joinedAt": int(entry.joined_at.timestamp() * 1000) if entry.joined_at else 0,
        "testerId": None,
    }


async def _db_resolve_queue(
    session: AsyncSession, kit: str
) -> tuple[Optional[object], Optional[object]]:
    kit_row = await KitRepository().get_by_name(session, kit)
    if kit_row is None:
        return None, None
    queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
    return kit_row, queue


async def _db_join_queue(
    session_factory, *, uid: str, username: str, ign: str, kit: str, now: int
) -> dict:
    for attempt in range(2):
        try:
            return await _db_join_queue_once(
                session_factory, uid=uid, username=username, ign=ign, kit=kit, now=now
            )
        except IntegrityError:
            if attempt == 0:
                continue
            raise
    raise RuntimeError("unreachable")


async def _db_join_queue_once(
    session_factory, *, uid: str, username: str, ign: str, kit: str, now: int
) -> dict:
    async with db_transaction(session_factory) as session:
        kit_row, queue = await _db_resolve_queue(session, kit)
        if kit_row is None or queue is None:
            return {"result": "closed"}

        try:
            _outcome, player = await PlayerRepository().claim_discord_id(
                session, discord_id=int(uid), ign=ign
            )
        except PlayerIdentityError:
            return {"result": "identity_conflict"}

        active = await CooldownRepository().get_active_waitlist(
            session,
            player_id=player.id,
            kit_id=kit_row.id,
            now=_db_dt(now),
        )
        if active:
            remaining = int((active[0].expires_at - _db_dt(now)).total_seconds() * 1000)
            return {"result": "cooldown", "remaining": max(remaining, 0)}

        existing = await QueueEntryRepository().get_waiting(
            session, queue_id=queue.id, player_id=player.id
        )
        if existing is not None:
            return {"result": "duplicate"}

        await QueueEntryRepository().enqueue(
            session,
            queue_id=queue.id,
            player_id=player.id,
            ign=ign,
            kit_id=kit_row.id,
            joined_at=_db_dt(now),
            username=username,
        )
        return {"result": "joined"}


async def join_queue(
    user_id: str,
    username: str,
    ign: str,
    kit: str,
    *,
    joined_at_ms: int,
    cooldown_ms: int,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> dict:
    """Transakčně přidá hráče do fronty (aktivní fronta + cooldown + duplicita).

    Vrací slovník s klíčem ``result``:
      - ``"joined"``    → hráč byl přidán,
      - ``"closed"``    → fronta už není aktivní,
      - ``"cooldown"``  → cooldown stále běží (klíč ``remaining`` = zbývající ms),
      - ``"duplicate"`` → hráč už ve frontě kitu je.
    """
    kit_key = str(kit).lower()
    uid = str(user_id)
    now = joined_at_ms

    if session_factory is not None:
        return await _db_join_queue(
            session_factory,
            uid=uid,
            username=username,
            ign=ign,
            kit=kit,
            now=now,
        )

    async def _run(tx):
        active_queues = tx.get("active_queues.json", {})
        if not active_queues.get(kit_key):
            return {"result": "closed"}

        cooldowns = tx.get("cooldowns.json", {})
        remaining = cooldown_remaining(cooldowns, uid, now, cooldown_ms)
        if remaining is not None:
            return {"result": "cooldown", "remaining": remaining}

        queue = tx.get("queue.json")
        if already_in_queue(queue, uid, kit_key):
            return {"result": "duplicate"}

        queue.append(make_entry(uid, username, ign, kit, now))
        tx.set("queue.json", queue)
        return {"result": "joined"}

    return await transaction(
        ("active_queues.json", "cooldowns.json", "queue.json"), _run
    )


async def _db_leave_queue(session_factory, *, uid: str, kit: str) -> bool:
    async with db_transaction(session_factory) as session:
        kit_row, queue = await _db_resolve_queue(session, kit)
        if kit_row is None or queue is None:
            return False
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return False
        entry = await QueueEntryRepository().get_waiting(
            session, queue_id=queue.id, player_id=player.id
        )
        if entry is None:
            return False
        await QueueEntryRepository().transition(
            session,
            entry_id=entry.id,
            status=QUEUE_ENTRY_LEFT,
            removed_at=datetime.now(timezone.utc),
            removed_reason="leave",
        )
        return True


async def leave_queue(
    user_id: str,
    kit_key: str,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> bool:
    """Transakčně vyjme hráče z fronty daného kitu. Vrací True, když byl odebrán."""
    kit_key = str(kit_key).lower()
    uid = str(user_id)

    if session_factory is not None:
        return await _db_leave_queue(session_factory, uid=uid, kit=kit_key)

    async def _run(tx):
        queue = tx.get("queue.json")
        new_queue = [
            p
            for p in queue
            if not (p.get("id") == uid and str(p.get("kit", "")).lower() == kit_key)
        ]
        if len(new_queue) == len(queue):
            return False
        tx.set("queue.json", new_queue)
        return True

    return await transaction(("queue.json",), _run)


async def _db_pop_for_kit(
    session_factory, *, kit: str, now: int
) -> Optional[dict]:
    async with db_transaction(session_factory) as session:
        kit_row, queue = await _db_resolve_queue(session, kit)
        if kit_row is None or queue is None:
            return None
        entries = await QueueEntryRepository().list_waiting(
            session, queue_id=queue.id
        )
        if not entries:
            return None
        entry = entries[0]
        await QueueEntryRepository().transition(
            session,
            entry_id=entry.id,
            status=QUEUE_ENTRY_PULLED,
            pulled_at=_db_dt(now),
        )
        player = await session.get(Player, entry.player_id)
        return _db_entry_to_dict(entry, player, kit_row.name)


async def pop_for_kit(
    kit_key: str,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
):
    """Transakčně odebere PRVNÍHO hráče kitu z fronty (pull).

    Vrací záznam hráče, nebo ``None``, když fronta kitu už nikoho nemá.
    """
    kit_key = str(kit_key).lower()

    if session_factory is not None:
        return await _db_pop_for_kit(
            session_factory, kit=kit_key, now=int(datetime.now(timezone.utc).timestamp() * 1000)
        )

    async def _run(tx):
        queue = tx.get("queue.json")
        index = next(
            (
                i
                for i, p in enumerate(queue)
                if str(p.get("kit", "")).lower() == kit_key
            ),
            None,
        )
        if index is None:
            return None
        player = queue.pop(index)
        tx.set("queue.json", queue)
        return player

    return await transaction(("queue.json",), _run)


async def _db_remove_by_player_id(session_factory, *, uid: str) -> bool:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return False
        entries = await QueueEntryRepository().list_waiting_for_player(
            session, player_id=player.id
        )
        if not entries:
            return False
        now = datetime.now(timezone.utc)
        for entry in entries:
            await QueueEntryRepository().transition(
                session,
                entry_id=entry.id,
                status=QUEUE_ENTRY_PULLED,
                pulled_at=now,
            )
        return True


async def remove_by_player_id(
    player_id: str,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> bool:
    """Transakčně vyjme hráče z fronty podle Discord ID (nezávisle na kitu).

    Vrací True, když byl ve frontě nalezen a odebrán.
    """
    uid = str(player_id)

    if session_factory is not None:
        return await _db_remove_by_player_id(session_factory, uid=uid)

    async def _run(tx):
        queue = tx.get("queue.json")
        new_queue = [p for p in queue if p.get("id") != uid]
        if len(new_queue) == len(queue):
            return False
        tx.set("queue.json", new_queue)
        return True

    return await transaction(("queue.json",), _run)


async def _db_save_pulled_player(
    session_factory, *, player: dict, channel_id: int
) -> None:
    async with db_transaction(session_factory) as session:
        uid = str(player.get("id", ""))
        if not uid:
            return
        player_row = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player_row is None:
            return
        pulled = await QueueEntryRepository().list_by_status(
            session, player_id=player_row.id, status=QUEUE_ENTRY_PULLED
        )
        if not pulled:
            return
        entry = pulled[0]
        await QueueEntryRepository().transition(
            session,
            entry_id=entry.id,
            status=QUEUE_ENTRY_PULLED,
            pulled_at=entry.pulled_at or datetime.now(timezone.utc),
            room_channel_id=channel_id,
        )


async def _db_preset_player_room(
    session_factory, *, player: dict, channel_id: int
) -> None:
    async with db_transaction(session_factory) as session:
        uid = str(player.get("id", ""))
        if not uid:
            return
        player_row = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player_row is None:
            return
        entries = await QueueEntryRepository().list_waiting_for_player(
            session, player_id=player_row.id
        )
        if not entries:
            return
        entry = entries[0]
        entry.room_channel_id = channel_id
        await session.flush()


async def preset_player_room(
    player: dict,
    channel_id: int,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> None:
    """Zapíše přednastavený přístup hráče do roomky/ticketu.

    JSON režim: ``pulled_players.json`` (jeden záznam na hráče, přepis záměrný).
    DB režim: ``room_channel_id`` na čekajícím záznamu hráče ve frontě
    (přednastavený přístup = hráč zůstává ve frontě); bez čekajícího záznamu
    se nic nezapíše — /result stejně odebere práva přepisem všech kanálů.
    """

    if session_factory is not None:
        return await _db_preset_player_room(
            session_factory, player=player, channel_id=channel_id
        )

    async def _run(tx):
        pulled = tx.get("pulled_players.json", {})
        pulled[str(player.get("id", ""))] = {
            "channel": str(channel_id),
            "player": {
                "id": str(player.get("id", "")),
                "username": player.get("username", ""),
                "ign": player.get("ign", ""),
                "kit": str(player.get("kit", "")),
                "joinedAt": player.get("joinedAt", 0),
            },
        }
        tx.set("pulled_players.json", pulled)

    return await transaction(("pulled_players.json",), _run)


async def save_pulled_player(
    player: dict,
    channel_id: int,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> None:
    """Zapíše/aktualizuje záznam vytaženého hráče (roomka, kde testuje).

    JSON režim: ``pulled_players.json`` (jeden záznam na hráče, přepis záměrný).
    DB režim: nejnovější ``pulled`` záznam hráče v ``queue_entries`` dostane
    ``room_channel_id``. Hráč bez aktivního záznamu (přednastavený přístup přes
    ``/mktesterroom`` mimo frontu) se v DB režimu nezaznamená – tu větu řeší
    rewiring cogs v todo #3.
    """

    if session_factory is not None:
        return await _db_save_pulled_player(session_factory, player=player, channel_id=channel_id)

    async def _run(tx):
        pulled = tx.get("pulled_players.json", {})
        pulled[str(player.get("id", ""))] = {
            "channel": str(channel_id),
            "player": {
                "id": str(player.get("id", "")),
                "username": player.get("username", ""),
                "ign": player.get("ign", ""),
                "kit": str(player.get("kit", "")),
                "joinedAt": player.get("joinedAt", 0),
            },
        }
        tx.set("pulled_players.json", pulled)

    return await transaction(("pulled_players.json",), _run)


async def _db_remove_pulled_player(session_factory, *, uid: str) -> bool:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return False
        pulled = await QueueEntryRepository().list_by_status(
            session, player_id=player.id, status=QUEUE_ENTRY_PULLED
        )
        if not pulled:
            return False
        await QueueEntryRepository().transition(
            session,
            entry_id=pulled[0].id,
            status=QUEUE_ENTRY_TESTED,
            removed_at=datetime.now(timezone.utc),
            removed_reason="tested",
        )
        return True


async def remove_pulled_player(
    player_id: str,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> bool:
    """Smaže záznam vytaženého hráče. Vrací True, když existoval."""
    uid = str(player_id)

    if session_factory is not None:
        return await _db_remove_pulled_player(session_factory, uid=uid)

    async def _run(tx):
        pulled = tx.get("pulled_players.json", {})
        if uid not in pulled:
            return False
        del pulled[uid]
        tx.set("pulled_players.json", pulled)
        return True

    return await transaction(("pulled_players.json",), _run)

# ---------------------------------------------------------------------------
# Active-queue lifecycle (todo #10b): /openq, /closeq, /joinasqueue, /leaveq,
# /queue list, /pull display, /removeq, /skip. JSON režim zachovává původní
# tvary (active_queues.json / queue.json / pulled_players.json) beze změny.
# ---------------------------------------------------------------------------


def _qdata(name: str, opener: Optional[str], testers: list[str], time_ms: int) -> dict:
    return {
        "name": name,
        "opener": opener,
        "testers": testers,
        "time": time_ms,
    }


async def _db_qdata(
    session: AsyncSession, queue: Queue
) -> dict:
    qt_repo = QueueTesterRepository()
    testers = await qt_repo.list(session, queue_id=queue.id)
    ids: list[str] = []
    for qt in testers:
        player = await session.get(Player, qt.player_id)
        ids.append(str(player.discord_id or player.id) if player is not None else str(qt.player_id))
    opener = ids[0] if ids else None
    return _qdata(
        queue.name,
        opener,
        ids,
        int(queue.started_at.timestamp() * 1000),
    )


async def _db_open_queue(
    session_factory,
    *,
    kit_key: str,
    name: str,
    opener_uid: str,
    opener_ign: str,
) -> tuple[str, Optional[dict]]:
    for attempt in range(2):
        try:
            return await _db_open_queue_once(
                session_factory,
                kit_key=kit_key,
                name=name,
                opener_uid=opener_uid,
                opener_ign=opener_ign,
            )
        except IntegrityError:
            if attempt == 0:
                continue
            raise
    raise RuntimeError("unreachable")


async def _db_open_queue_once(
    session_factory,
    *,
    kit_key: str,
    name: str,
    opener_uid: str,
    opener_ign: str,
) -> tuple[str, Optional[dict]]:
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, name)
        if kit_row is None:
            return ("unknown_kit", None)
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is not None:
            return ("exists", await _db_qdata(session, queue))
        try:
            _outcome, player = await PlayerRepository().claim_discord_id(
                session, discord_id=int(opener_uid), ign=opener_ign
            )
        except PlayerIdentityError:
            return ("identity_conflict", None)
        queue = await QueueRepository().open(
            session, kit_id=kit_row.id, name=name
        )
        await QueueTesterRepository().add(
            session, queue_id=queue.id, player_id=player.id
        )
        return ("ok", await _db_qdata(session, queue))


async def open_queue(
    kit_key: str,
    name: str,
    opener_uid: str,
    opener_ign: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> tuple[str, Optional[dict]]:
    """Transakčně otevře frontu kitu (jen jednu aktivní na kit).

    Vrací ``(status, qdata)``: status in ``ok`` / ``exists`` /
    ``unknown_kit`` / ``identity_conflict``; qdata = JSON tvar
    ``active_queues[kit_key]`` (name, opener, testers, time).
    """
    kit_key = str(kit_key).lower()

    if session_factory is not None:
        return await _db_open_queue(
            session_factory,
            kit_key=kit_key,
            name=name,
            opener_uid=opener_uid,
            opener_ign=opener_ign,
        )

    async def _run(tx):
        active_queues = tx.get("active_queues.json", {})
        if kit_key in active_queues:
            return ("exists", active_queues[kit_key])
        qdata = {
            "name": name,
            "opener": opener_uid,
            "testers": [opener_uid],
            "time": int(time.time() * 1000),
        }
        active_queues[kit_key] = qdata
        tx.set("active_queues.json", active_queues)
        return ("ok", qdata)

    return await transaction(("active_queues.json",), _run)


async def set_queue_panel(
    kit_key: str,
    channel_id: int,
    message_id: int,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> None:
    """Zapíše panel fronty (channel + message) pro re-registraci po restartu."""
    kit_key = str(kit_key).lower()
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            kit_row = await KitRepository().get_by_name(session, kit_key)
            if kit_row is None:
                return
            queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
            if queue is not None:
                queue.panel_channel_id = int(channel_id)
                queue.panel_message_id = int(message_id)
                await session.flush()
        return

    async def _run(tx):
        queue_messages = tx.get("queue_messages.json", {})
        queue_messages[kit_key] = {"message_id": str(message_id), "kit": kit_key}
        tx.set("queue_messages.json", queue_messages)

    return await transaction(("queue_messages.json",), _run)


async def _db_close_queue(session_factory, *, kit_key: str) -> Optional[dict]:
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return None
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None:
            return None
        old_panel = (
            {"message_id": str(queue.panel_message_id)}
            if queue.panel_message_id is not None
            else None
        )
        entries = await QueueEntryRepository().list_waiting(
            session, queue_id=queue.id
        )
        for entry in entries:
            await QueueEntryRepository().transition(
                session,
                entry_id=entry.id,
                status=QUEUE_ENTRY_LEFT,
                removed_at=datetime.now(timezone.utc),
                removed_reason="queue_closed",
            )
        await QueueRepository().close(session, queue_id=queue.id)
        return old_panel


async def close_queue(
    kit_key: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Optional[dict]:
    """Transakčně zavře frontu: promazá čekající hráče + panel záznam.

    Vrací záznam panelu ({message_id}) nebo None.
    """
    kit_key = str(kit_key).lower()

    if session_factory is not None:
        return await _db_close_queue(session_factory, kit_key=kit_key)

    async def _close(tx):
        active = tx.get("active_queues.json", {})
        active.pop(kit_key, None)
        tx.set("active_queues.json", active)

        queue = tx.get("queue.json")
        remaining = [p for p in queue if str(p.get("kit", "")).lower() != kit_key]
        if len(remaining) != len(queue):
            tx.set("queue.json", remaining)

        messages = tx.get("queue_messages.json", {})
        old = messages.pop(kit_key, None)
        tx.set("queue_messages.json", messages)
        return old

    return await transaction(
        ("active_queues.json", "queue.json", "queue_messages.json"), _close
    )


async def queue_state(
    kit_key: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Optional[dict]:
    """Aktivní fronta kitu v JSON tvaru (name/opener/testers/time) nebo None."""
    kit_key = str(kit_key).lower()

    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            kit_row = await KitRepository().get_by_name(session, kit_key)
            if kit_row is None:
                return None
            queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
            if queue is None:
                return None
            return await _db_qdata(session, queue)

    return _json_active_queues().get(kit_key)


def _json_active_queues() -> dict:
    from storage import load_data

    return load_data("active_queues.json", {}) or {}


async def active_queues(
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> dict:
    """Mapa {kit_key: qdata} všech aktivních front (for /queue list + panel)."""
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            queues = await QueueRepository().list_active(session)
            result = {}
            for queue in queues:
                kit = await session.get(Kit, queue.kit_id)
                if kit is None:
                    continue
                result[kit.key] = await _db_qdata(session, queue)
            return result

    return _json_active_queues()


async def list_queue_entries(
    kit_key: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> list[dict]:
    """Čekající hráči kitu (JSON tvar entry dictů; queue.json filtr v JSON režimu)."""
    kit_key = str(kit_key).lower()

    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            kit_row = await KitRepository().get_by_name(session, kit_key)
            if kit_row is None:
                return []
            queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
            if queue is None:
                return []
            entries = await QueueEntryRepository().list_waiting(
                session, queue_id=queue.id
            )
            players = {
                p.id: p
                for p in (
                    await session.execute(
                        select(Player).where(
                            Player.id.in_([e.player_id for e in entries])
                        )
                    )
                ).scalars()
            }
            return [
                _db_entry_to_dict(e, players.get(e.player_id), kit_row.name)
                for e in entries
            ]

    from storage import load_data

    queue = load_data("queue.json") or []
    return [p for p in queue if str(p.get("kit", "")).lower() == kit_key]


async def queue_snapshot(
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> tuple[list[dict], dict]:
    """(celá global fronta, aktivní fronty) pro /queue list — JSON tvar."""
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            queues = await QueueRepository().list_active(session)
            entries: list[dict] = []
            for queue in queues:
                kit = await session.get(Kit, queue.kit_id)
                if kit is None:
                    continue
                for e in await QueueEntryRepository().list_waiting(
                    session, queue_id=queue.id
                ):
                    player = await session.get(Player, e.player_id)
                    entries.append(_db_entry_to_dict(e, player, kit.name))
            entries.sort(key=lambda d: d["joinedAt"])
            result: dict = {}
            for queue in queues:
                kit = await session.get(Kit, queue.kit_id)
                if kit is None:
                    continue
                result[kit.key] = await _db_qdata(session, queue)
            return entries, result

    from storage import load_data

    return load_data("queue.json") or [], _json_active_queues()


async def peek_first_player(
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Optional[dict]:
    """První čekající hráč napříč aktivními frontami (/pull display)."""
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            stmt = (
                select(QueueEntry, Queue, Kit)
                .join(Queue, Queue.id == QueueEntry.queue_id)
                .join(Kit, Kit.id == QueueEntry.kit_id)
                .where(
                    Queue.closed_at.is_(None),
                    QueueEntry.status == "waiting",
                )
                .order_by(QueueEntry.joined_at.asc(), QueueEntry.id.asc())
                .limit(1)
            )
            row = (
                await session.execute(stmt)
            ).first()
            if row is None:
                return None
            entry, _queue, kit = row
            player = await session.get(Player, entry.player_id)
            return _db_entry_to_dict(entry, player, kit.name)

    from storage import load_data

    queue = load_data("queue.json") or []
    return queue[0] if queue else None


async def _db_join_queue_tester(
    session_factory, *, kit_key: str, uid: str, ign: str
) -> tuple[str, Optional[dict]]:
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return ("closed", None)
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None:
            return ("closed", None)
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            other = await PlayerRepository().get_by_ign(session, ign)
            if other is not None and other.discord_id is not None:
                return ("closed", None)
            try:
                _outcome, player = await PlayerRepository().claim_discord_id(
                    session, discord_id=int(uid), ign=ign
                )
            except PlayerIdentityError:
                return ("closed", None)
        if await QueueTesterRepository().is_member(
            session, queue_id=queue.id, player_id=player.id
        ):
            return ("duplicate", await _db_qdata(session, queue))
        await QueueTesterRepository().add(
            session, queue_id=queue.id, player_id=player.id
        )
        return ("ok", await _db_qdata(session, queue))


async def join_queue_tester(
    kit_key: str,
    uid: str,
    ign: str = "",
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> tuple[str, Optional[dict]]:
    """Přidá testera k aktivní frontě (joinasqueue). Status ok/closed/duplicate."""
    kit_key = str(kit_key).lower()
    uid = str(uid)

    if session_factory is not None:
        return await _db_join_queue_tester(
            session_factory, kit_key=kit_key, uid=uid, ign=ign
        )

    async def _join(tx):
        active_queues = tx.get("active_queues.json", {})
        qdata = active_queues.get(kit_key)
        if not qdata:
            return ("closed", None)
        if uid in qdata.get("testers", []):
            return ("duplicate", qdata)
        qdata.setdefault("testers", []).append(uid)
        tx.set("active_queues.json", active_queues)
        return ("ok", qdata)

    return await transaction(("active_queues.json",), _join)


async def _db_leave_queue_tester(
    session_factory, *, kit_key: str, uid: str
) -> tuple[str, Optional[dict]]:
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return ("closed", None)
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None:
            return ("closed", None)
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return ("not_listed", None)
        if not await QueueTesterRepository().remove(
            session, queue_id=queue.id, player_id=player.id
        ):
            return ("not_listed", None)
        return ("ok", await _db_qdata(session, queue))


async def leave_queue_tester(
    kit_key: str,
    uid: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> tuple[str, Optional[dict]]:
    """Odebere testera z aktivní fronty. Status ok/closed/not_listed."""
    kit_key = str(kit_key).lower()
    uid = str(uid)

    if session_factory is not None:
        return await _db_leave_queue_tester(
            session_factory, kit_key=kit_key, uid=uid
        )

    async def _leave(tx):
        active_queues = tx.get("active_queues.json", {})
        qdata = active_queues.get(kit_key)
        if not qdata:
            return ("closed", None)
        testers = qdata.get("testers", [])
        if uid not in testers:
            return ("not_listed", qdata)
        testers.remove(uid)
        if qdata.get("opener") == uid and testers:
            qdata["opener"] = testers[0]
        tx.set("active_queues.json", active_queues)
        return ("ok", qdata)

    return await transaction(("active_queues.json",), _leave)


async def _db_register_global_tester(
    session_factory, *, uid: str, ign: str
) -> bool:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            try:
                _outcome, player = await PlayerRepository().claim_discord_id(
                    session, discord_id=int(uid), ign=ign
                )
            except PlayerIdentityError:
                return False
        if await TesterRepository().is_tester(session, player_id=player.id):
            return True
        return await TesterRepository().grant(
            session, player_id=player.id, granted_by=int(uid)
        ) is not None


async def register_global_tester(
    uid: str,
    ign: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> bool:
    """Zaregistruje globálně aktivního testera (joinastester).

    JSON režim: testers.json append. DB režim: `testers` řádek (graceful
    no-op při už existujícím — unique PK).
    """
    uid = str(uid)

    if session_factory is not None:
        return await _db_register_global_tester(
            session_factory, uid=uid, ign=ign
        )

    from storage import load_data, save_data

    testers = load_data("testers.json")
    if uid in testers:
        return True
    testers.append(uid)
    save_data("testers.json", testers)
    return True


async def _db_removeq(session_factory, *, uid: str) -> Optional[dict]:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return None
        entries = await QueueEntryRepository().list_waiting_for_player(
            session, player_id=player.id
        )
        if not entries:
            return None
        entry = entries[0]
        kit = await session.get(Kit, entry.kit_id)
        out = _db_entry_to_dict(entry, player, kit.name if kit is not None else "")
        await QueueEntryRepository().transition(
            session,
            entry_id=entry.id,
            status=QUEUE_ENTRY_PULLED,
            pulled_at=datetime.now(timezone.utc),
        )
        return out


async def removeq(
    uid: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Optional[dict]:
    """Vyhození hráče z fronty (/removeq). Vrací záznam hráče nebo None."""
    uid = str(uid)

    if session_factory is not None:
        return await _db_removeq(session_factory, uid=uid)

    async def _run(tx):
        queue = tx.get("queue.json")
        entry = next((p for p in queue if p.get("id") == uid), None)
        if entry is None:
            return None
        tx.set("queue.json", [p for p in queue if p.get("id") != uid])
        return entry

    return await transaction(("queue.json",), _run)


async def _db_skip_player(session_factory, *, uid: str) -> dict:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return {"channel_id": None, "kit_key": "", "was_pulled": False,
                    "requeued": False, "moved": False, "next": None}
        pulled = await QueueEntryRepository().list_by_status(
            session, player_id=player.id, status=QUEUE_ENTRY_PULLED
        )
        waiting = await QueueEntryRepository().list_waiting_for_player(
            session, player_id=player.id
        )
        entry = pulled[0] if pulled else (waiting[0] if waiting else None)
        if entry is None:
            return {"channel_id": None, "kit_key": "", "was_pulled": False,
                    "requeued": False, "moved": False, "next": None}
        channel_id = entry.room_channel_id
        kit = await session.get(Kit, entry.kit_id)
        kit_key = kit.key if kit is not None else ""
        queue = await session.get(Queue, entry.queue_id)
        was_pulled = bool(pulled)
        in_queue = bool(waiting)
        if pulled:
            await QueueEntryRepository().transition(
                session,
                entry_id=entry.id,
                status="waiting",
                pulled_at=None,
                room_channel_id=None,
            )
            entry.position = await QueueEntryRepository().next_position(
                session, queue_id=entry.queue_id
            )
            await session.flush()
        requeued = was_pulled and not in_queue
        moved = in_queue
        next_entry = None
        if queue is not None:
            listed = await QueueEntryRepository().list_waiting(
                session, queue_id=queue.id
            )
            if listed:
                n_player = await session.get(Player, listed[0].player_id)
                next_entry = _db_entry_to_dict(
                    listed[0], n_player, kit.name if kit is not None else ""
                )
        return {"channel_id": channel_id, "kit_key": kit_key,
                "was_pulled": was_pulled, "requeued": requeued,
                "moved": moved, "next": next_entry}


async def skip_player(
    uid: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> dict:
    """Přeskočení AFK hráče: přesun na konec fronty (+ záznam roomky /skip).

    Vrací {channel_id, kit_key, was_pulled, requeued, moved, next}.
    """
    uid = str(uid)

    if session_factory is not None:
        return await _db_skip_player(session_factory, uid=uid)

    async def _run(tx):
        pulled = tx.get("pulled_players.json", {})
        entry = pulled.get(uid)
        channel_id_raw = None
        stored_player = None
        kit_key = ""
        was_pulled = False
        if entry is not None:
            was_pulled = True
            if isinstance(entry, dict):
                channel_id_raw = entry.get("channel")
                stored_player = entry.get("player")
                if isinstance(stored_player, dict):
                    kit_key = str(stored_player.get("kit", "")).lower()
            else:
                channel_id_raw = entry
            del pulled[uid]
            tx.set("pulled_players.json", pulled)

        queue = tx.get("queue.json")
        new_queue, new_kit_key, moved = move_to_queue_end(queue, stored_player, uid)
        if not kit_key:
            kit_key = new_kit_key
        requeued = len(new_queue) != len(queue)
        if requeued:
            tx.set("queue.json", new_queue)
        next_player = next(
            (p for p in new_queue if str(p.get("kit", "")).lower() == kit_key), None
        )
        return {
            "channel_id": int(channel_id_raw) if channel_id_raw else None,
            "kit_key": kit_key,
            "was_pulled": was_pulled,
            "requeued": requeued,
            "moved": moved,
            "next": next_player,
        }

    return await transaction(("pulled_players.json", "queue.json"), _run)


async def panel_message_id(
    kit_key: str,
    *,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> Optional[str]:
    """ID zprávy panelu kitu (pro update_panel) nebo None."""
    kit_key = str(kit_key).lower()

    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            kit_row = await KitRepository().get_by_name(session, kit_key)
            if kit_row is None:
                return None
            queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
            if queue is None or queue.panel_message_id is None:
                return None
            return str(queue.panel_message_id)

    from storage import load_data

    entry = (load_data("queue_messages.json", {}) or {}).get(kit_key)
    return entry.get("message_id") if isinstance(entry, dict) else entry

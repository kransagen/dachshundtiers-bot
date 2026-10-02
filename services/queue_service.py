"""Čistá logika front + transakční operace – výhradně PostgreSQL.

Veškeré změny probíhají atomicky přes jednu PostgreSQL transakci
(``db_transaction``). Souběžné interakce nemůžou duplicitně zapsat hráče do
fronty, obejít cooldown ani ztratit zápis (join vs. pull vs. leave) a vytažení
(pull) hráče už není „dvakrát" (FOR UPDATE SKIP LOCKED).

Legacy JSON režim (``services.store`` / ``active_queues.json``,
``queue.json``, ``pulled_players.json``, ``queue_messages.json``,
``testers.json``, ``cooldowns.json``) byl zcela odebrán: tyto funkce
vyžadují ``session_factory`` a stav front je v PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.exc import IntegrityError

from db.models import Kit, Player, Queue, QueueEntry
from db.repositories.cooldowns import CooldownRepository
from db.repositories.kits import KitRepository, KitTesterRoomRepository
from db.repositories.players import PlayerRepository
from db.repositories.queues import (
    QUEUE_ENTRY_LEFT,
    QUEUE_ENTRY_PULLED,
    QUEUE_ENTRY_TESTED,
    QUEUE_ENTRY_WAITING,
    QueueEntryRepository,
    QueueRepository,
    QueueTesterRepository,
)
from db.repositories.evaluations import TesterRepository
from db.services.session import transaction as db_transaction


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
    session_factory, *, uid: str, username: str, kit: str, now: int
) -> dict:
    for attempt in range(2):
        try:
            return await _db_join_queue_once(
                session_factory, uid=uid, username=username, kit=kit, now=now
            )
        except IntegrityError:
            if attempt == 0:
                continue
            raise
    raise RuntimeError("unreachable")


async def _db_join_queue_once(
    session_factory, *, uid: str, username: str, kit: str, now: int
) -> dict:
    async with db_transaction(session_factory) as session:
        kit_row, queue = await _db_resolve_queue(session, kit)
        if kit_row is None or not kit_row.active or queue is None:
            return {"result": "closed"}

        # E4 audit fix: ``next_position`` is MAX(position)+1 — a read of
        # shared state. Two concurrent joins (distinct players) both read the
        # same MAX and would both insert the SAME position unless this
        # transaction serializes on the queue row first (there is no unique
        # index on (queue_id, position) to reject the loser, and then the
        # queue's FIFO order would silently flip). The FOR UPDATE lock on the
        # queue row is held to commit, so each concurrent join computes the
        # next position after the previous one commits — EXACTLY like the
        # advisory lock used for mirror apply (db/repositories/tiers.py).
        # The same lock serializes join against close: a queue closed while
        # this join waited for the lock is seen as closed here.
        locked = await QueueRepository().lock(session, queue_id=queue.id)
        if locked is None or locked.closed_at is not None:
            return {"result": "closed"}

        # Do fronty jen s propojeným IGN (/linkign) – IGN z fronty se tak
        # vždy shoduje s hráčem, kterému patří historie tierů.
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None or player.ign_linked_at is None:
            return {"result": "not_linked"}
        ign = player.ign

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
        return {"result": "joined", "ign": ign}


async def join_queue(
    user_id: str,
    username: str,
    kit: str,
    *,
    joined_at_ms: int,
    cooldown_ms: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict:
    """Transakčně přidá hráče do fronty (aktivní fronta + cooldown + duplicita).
    Vyžaduje PostgreSQL (``session_factory``).

    Vrací slovník s klíčem ``result``:
      - ``"joined"``    → hráč byl přidán (s propojeným IGN, klíč ``ign``),
      - ``"not_linked"``→ hráč nemá propojené IGN (/linkign),
      - ``"closed"``    → fronta už není aktivní,
      - ``"cooldown"``  → cooldown stále běží (klíč ``remaining`` = zbývající ms),
      - ``"duplicate"`` → hráč už ve frontě kitu je.
    """
    uid = str(user_id)
    now = joined_at_ms
    return await _db_join_queue(
        session_factory,
        uid=uid,
        username=username,
        kit=str(kit).lower(),
        now=now,
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
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Transakčně vyjme hráče z fronty daného kitu. Vrací True, když byl odebrán."""
    kit_key = str(kit_key).lower()
    uid = str(user_id)
    return await _db_leave_queue(session_factory, uid=uid, kit=kit_key)


async def _db_pop_for_kit(
    session_factory, *, kit: str, now: int
) -> Optional[dict]:
    async with db_transaction(session_factory) as session:
        kit_row, queue = await _db_resolve_queue(session, kit)
        if kit_row is None or queue is None:
            return None
        # Single conditional UPDATE (FOR UPDATE SKIP LOCKED) instead of
        # "read first waiting, then update it": two concurrent pulls used to
        # both read the same row and both report success, so one player could
        # be handed to two testers. See QueueEntryRepository.claim_next_waiting.
        entry = await QueueEntryRepository().claim_next_waiting(
            session, queue_id=queue.id, pulled_at=_db_dt(now)
        )
        if entry is None:
            return None
        player = await session.get(Player, entry.player_id)
        return _db_entry_to_dict(entry, player, kit_row.name)


async def pop_for_kit(
    kit_key: str,
    session_factory: async_sessionmaker[AsyncSession],
):
    """Transakčně odebere PRVNÍHO hráče kitu z fronty (pull, legacy helper).

    Vrací záznam hráče, nebo ``None``, když fronta kitu už nikoho nemá.
    Nové proudy používají ``pull_for_kit`` (roomka + fronta v jedné transakci).
    """
    kit_key = str(kit_key).lower()
    return await _db_pop_for_kit(
        session_factory, kit=kit_key, now=int(datetime.now(timezone.utc).timestamp() * 1000)
    )


# ---------------------------------------------------------------------------
# Tester roomky: kit -> Discord kanál (autoritativní mapování v PostgreSQL)
# ---------------------------------------------------------------------------
# Dřív tester vybíral roomku ručně (ChannelSelect) a mapování mezi kitem a
# pokojem neexistovalo. Teď je mapování řádek v `kit_tester_rooms`, který
# založí `/mktesterroom <kit>` a ze kterého ho `/queue pull <kit>` čte. Kanál
# je jen projekce do Discordu; autoritou je řádek v DB.


async def set_tester_room(
    kit: str,
    channel_id: int,
    *,
    created_by: Optional[int] = None,
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[int]:
    """Přiřadí roomku kitu; vrací ``kit_id`` nebo ``None`` pro neznámý kit.

    Idempotentní: opakované volání stejného kitu *nahradí* kanál, nevytvoří
    druhý řádek (upsert na `kit_id`). Pokud už tenhle kanál patří jinému
    kitu, UNIQUE na ``channel_id`` to odmítne — dvě kity si jednu roomku
    nemůžou vzít, jinak by `/queue pull` nemělo jednoznačnou odpověď.
    """
    kit_key = (kit or "").strip().lower()
    if not kit_key:
        return None
    async with db_transaction(session_factory) as session:
        kit_row = await _resolve_kit_any(session, kit_key)
        if kit_row is None:
            return None
        await KitTesterRoomRepository().set_room(
            session,
            kit_id=kit_row.id,
            channel_id=channel_id,
            created_by=created_by,
        )
        return kit_row.id


async def resolve_tester_room(
    kit: str, *, session_factory: async_sessionmaker[AsyncSession]
) -> Optional[int]:
    """``channel_id`` tester roomky pro kit, nebo ``None`` když není zadaná."""
    kit_key = (kit or "").strip().lower()
    if not kit_key:
        return None
    async with db_transaction(session_factory) as session:
        kit_row = await _resolve_kit_any(session, kit_key)
        if kit_row is None:
            return None
        room = await KitTesterRoomRepository().get_for_kit(session, kit_id=kit_row.id)
        return room.channel_id if room is not None else None


async def clear_tester_room(
    kit: str, *, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    kit_key = (kit or "").strip().lower()
    if not kit_key:
        return False
    async with db_transaction(session_factory) as session:
        kit_row = await _resolve_kit_any(session, kit_key)
        if kit_row is None:
            return False
        return await KitTesterRoomRepository().clear(session, kit_id=kit_row.id)


async def clear_tester_room_for_channel(
    channel_id: int, *, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    """Smaže mapování roomky podle kanálu (kanál už neexistuje)."""
    async with db_transaction(session_factory) as session:
        room = await KitTesterRoomRepository().get_for_channel(
            session, channel_id=int(channel_id)
        )
        if room is None:
            return False
        return await KitTesterRoomRepository().clear(session, kit_id=room.kit_id)


async def is_tester_room(
    channel_id: int, *, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    """Je kanál zaregistrovaná tester roomka nějakého kitu?"""
    async with db_transaction(session_factory) as session:
        return (
            await KitTesterRoomRepository().get_for_channel(
                session, channel_id=int(channel_id)
            )
            is not None
        )


async def _resolve_kit_any(session: AsyncSession, kit_key: str) -> Optional[object]:
    """Kit by key, s fallbackem na case-insensitive display name.

    Stejné pořadí jako u ostatních resolve helperů: `Kit.key` je primární
    business klíč, ale testeři i starší příkazy občas posílají display name.
    """
    repo = KitRepository()
    kit_row = await repo.get_by_key(session, kit_key)
    if kit_row is None:
        kit_row = await repo.get_by_name(session, kit_key)
    return kit_row


async def resolve_kit(
    kit: str, *, session_factory: async_sessionmaker[AsyncSession]
) -> Optional[object]:
    """Veřejný resolve helper: kit key nebo display name -> Kit řádek.

    Používají ho cogy, které potřebují potvrdit, že zadaný kit existuje,
    než s ním začnou něco dělat (`/mktesterroom`). Vyhazuje ``None``, ne
    hází — volající chce vědět, jestli kit zná, a píše o tom hlášku hráči.
    """
    kit_key = (kit or "").strip().lower()
    if not kit_key:
        return None
    async with db_transaction(session_factory) as session:
        return await _resolve_kit_any(session, kit_key)


# ---------------------------------------------------------------------------
# Kanonický pull: fronta kitu + automatické dohledání tester roomky
# ---------------------------------------------------------------------------

PULL_OK = "ok"
PULL_EMPTY = "empty"
PULL_NO_ROOM = "no_tester_room"
PULL_NO_KIT = "no_such_kit"


@dataclass(frozen=True)
class PullResult:
    """Výsledek ``pull_for_kit`` — strukturovaný místo řetězce."""

    status: str
    player: Optional[dict] = None
    channel_id: Optional[int] = None
    kit_name: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == PULL_OK


async def _db_pull_for_kit(
    session_factory, *, kit: str, now: int
) -> PullResult:
    async with db_transaction(session_factory) as session:
        kit_row = await _resolve_kit_any(session, kit)
        if kit_row is None or not kit_row.active:
            return PullResult(status=PULL_NO_KIT)

        # Roomku řešíme PRVNÍ. Když chybí, hráče z fronty vůbec nevybavíme —
        # vzít ho z fronty a pak zjistit, že nemáme kam ho pustit, by znamenalo
        # ztraceného hráče.
        room = await KitTesterRoomRepository().get_for_kit(session, kit_id=kit_row.id)
        if room is None:
            return PullResult(status=PULL_NO_ROOM, kit_name=kit_row.name)

        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None:
            return PullResult(status=PULL_EMPTY, kit_name=kit_row.name)

        # Přechod waiting -> pulled je jeden podmíněný UPDATE
        # (FOR UPDATE SKIP LOCKED), takže dvě souběžná pullnutí vyberou dva
        # různé hráče a žádný se nevytáhne dvakrát.
        entry = await QueueEntryRepository().claim_next_waiting(
            session, queue_id=queue.id, pulled_at=_db_dt(now)
        )
        if entry is None:
            return PullResult(status=PULL_EMPTY, kit_name=kit_row.name)
        player = await session.get(Player, entry.player_id)
        return PullResult(
            status=PULL_OK,
            player=_db_entry_to_dict(entry, player, kit_row.name),
            channel_id=room.channel_id,
            kit_name=kit_row.name,
        )


async def pull_for_kit(
    kit: str, *, session_factory: Optional[async_sessionmaker[AsyncSession]]
) -> PullResult:
    """Vytažení PRVNÍHO hráče kitu do jeho tester roomky — jedna transakce.

    Tohle je jediná kanonická cesta pro „vytáhnout hráče na test". Roomka se
    už nebere od testera, ale z tabulky ``kit_tester_rooms``; chybí-li, vrátí
    ``no_tester_room`` a hráč ve frontě zůstane.
    """
    kit_key = (kit or "").strip().lower()
    if not kit_key:
        return PullResult(status=PULL_NO_KIT)
    return await _db_pull_for_kit(
        session_factory, kit=kit_key, now=int(datetime.now(timezone.utc).timestamp() * 1000)
    )


async def _db_requeue_pulled_player(session_factory, *, uid: str, kit: str) -> bool:
    async with db_transaction(session_factory) as session:
        _kit_row, queue = await _db_resolve_queue(session, kit)
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if queue is None or player is None:
            return False
        pulled = await QueueEntryRepository().list_by_status(
            session, player_id=player.id, status=QUEUE_ENTRY_PULLED
        )
        entry = next((e for e in pulled if e.queue_id == queue.id), None)
        if entry is None:
            return False
        if (
            await QueueEntryRepository().get_waiting(
                session, queue_id=queue.id, player_id=player.id
            )
            is not None
        ):
            await QueueEntryRepository().transition(
                session,
                entry_id=entry.id,
                status=QUEUE_ENTRY_LEFT,
                removed_at=datetime.now(timezone.utc),
                removed_reason="room_missing",
            )
            return False
        entry.status = QUEUE_ENTRY_WAITING
        entry.pulled_at = None
        entry.room_channel_id = None
        await session.flush()
        return True


async def requeue_pulled_player(
    player: dict,
    kit: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Vrátí právě vytaženého hráče na jeho místo ve frontě (roomka nebyla k nalezení).

    Vrací True, když je hráč zase ``waiting``. Pokud se mezitím zapsal znovu,
    starý záznam se jen uzavře a vrací False.
    """
    uid = str(player.get("id", ""))
    if not uid:
        return False
    return await _db_requeue_pulled_player(
        session_factory, uid=uid, kit=str(kit).lower()
    )


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
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Transakčně označí čekající záznamy hráče (nezávisle na kitu) jako vytažené.

    Záznam dostane stav ``pulled`` bez roomky. Vrací True, když byl ve frontě nalezen.
    """
    uid = str(player_id)
    return await _db_remove_by_player_id(session_factory, uid=uid)


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
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Zapíše přednastavený přístup hráče do roomky/ticketu.

    ``room_channel_id`` na čekajícím záznamu hráče ve frontě (přednastavený
    přístup = hráč zůstává ve frontě); bez čekajícího záznamu se nic nezapíše
    — /result stejně odebere práva přepisem všech kanálů. PostgreSQL.
    """
    return await _db_preset_player_room(
        session_factory, player=player, channel_id=channel_id
    )


async def save_pulled_player(
    player: dict,
    channel_id: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Zapíše/aktualizuje záznam vytaženého hráče (roomka, kde testuje).

    Nejnovější ``pulled`` záznam hráče v ``queue_entries`` dostane
    ``room_channel_id``. Hráč bez aktivního záznamu (přednastavený přístup přes
    ``/mktesterroom`` mimo frontu) se nezaznamená. PostgreSQL.
    """
    return await _db_save_pulled_player(
        session_factory, player=player, channel_id=channel_id
    )


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
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Smaže záznam vytaženého hráče (pulled → tested). Vrací True, když existoval."""
    uid = str(player_id)
    return await _db_remove_pulled_player(session_factory, uid=uid)

# ---------------------------------------------------------------------------
# Active-queue lifecycle (todo #10b): /openq, /closeq, /joinasqueue, /leaveq,
# /queue list, /pull display, /removeq, /skip. Vše v PostgreSQL.
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
        if kit_row is None or not kit_row.active:
            return ("unknown_kit", None)
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is not None:
            return ("exists", await _db_qdata(session, queue))
        player = await PlayerRepository().get_or_create_shell(
            session, discord_id=int(opener_uid)
        )
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
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, Optional[dict]]:
    """Transakčně otevře frontu kitu (jen jednu aktivní na kit). PostgreSQL.

    Vrací ``(status, qdata)``: status in ``ok`` / ``exists`` /
    ``unknown_kit``; qdata = JSON tvar
    ``active_queues[kit_key]`` (name, opener, testers, time).
    """
    kit_key = str(kit_key).lower()
    return await _db_open_queue(
        session_factory,
        kit_key=kit_key,
        name=name,
        opener_uid=opener_uid,
        opener_ign=opener_ign,
    )


async def set_queue_panel(
    kit_key: str,
    channel_id: int,
    message_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Zapíše panel fronty (channel + message) pro re-registraci po restartu."""
    kit_key = str(kit_key).lower()
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is not None:
            queue.panel_channel_id = int(channel_id)
            queue.panel_message_id = int(message_id)
            await session.flush()


async def _db_close_queue(session_factory, *, kit_key: str) -> Optional[dict]:
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return None
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None:
            return None
        queue = await QueueRepository().lock(session, queue_id=queue.id)
        if queue is None or queue.closed_at is not None:
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
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[dict]:
    """Transakčně zavře frontu: promazá čekající hráče + panel záznam. PostgreSQL.

    Vrací záznam panelu ({message_id}) nebo None.
    """
    kit_key = str(kit_key).lower()
    return await _db_close_queue(session_factory, kit_key=kit_key)


async def queue_state(
    kit_key: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[dict]:
    """Aktivní fronta kitu v JSON tvaru (name/opener/testers/time) nebo None."""
    kit_key = str(kit_key).lower()
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return None
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None:
            return None
        return await _db_qdata(session, queue)


async def active_queues(
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict:
    """Mapa {kit_key: qdata} všech aktivních front (for /queue list + panel)."""
    async with db_transaction(session_factory) as session:
        queues = await QueueRepository().list_active(session)
        result = {}
        for queue in queues:
            kit = await session.get(Kit, queue.kit_id)
            if kit is None:
                continue
            result[kit.key] = await _db_qdata(session, queue)
        return result


async def list_queue_entries(
    kit_key: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> list[dict]:
    """Čekající hráči kitu (JSON tvar entry dictů). PostgreSQL."""
    kit_key = str(kit_key).lower()
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


async def queue_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[list[dict], dict]:
    """(celá global fronta, aktivní fronty) pro /queue list — JSON tvar."""
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


async def peek_first_player(
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[dict]:
    """První čekající hráč napříč aktivními frontami (/pull display)."""
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


async def _retry_on_integrity(fn, **kwargs):
    """Souběžné vytvoření „shell“ hráče: poražená transakce se zopakuje."""
    try:
        return await fn(**kwargs)
    except IntegrityError:
        return await fn(**kwargs)


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
        player = await PlayerRepository().get_or_create_shell(
            session, discord_id=int(uid)
        )
        if await QueueTesterRepository().is_member(
            session, queue_id=queue.id, player_id=player.id
        ):
            return ("duplicate", await _db_qdata(session, queue))
        try:
            async with session.begin_nested():
                await QueueTesterRepository().add(
                    session, queue_id=queue.id, player_id=player.id
                )
        except IntegrityError:
            return ("duplicate", await _db_qdata(session, queue))
        return ("ok", await _db_qdata(session, queue))


async def join_queue_tester(
    kit_key: str,
    uid: str,
    ign: str = "",
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, Optional[dict]]:
    """Přidá testera k aktivní frontě (joinasqueue). Status ok/closed/duplicate."""
    kit_key = str(kit_key).lower()
    uid = str(uid)
    return await _retry_on_integrity(
        _db_join_queue_tester,
        session_factory=session_factory,
        kit_key=kit_key,
        uid=uid,
        ign=ign,
    )


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
        testers = await QueueTesterRepository().list(session, queue_id=queue.id)
        if not any(t.player_id == player.id for t in testers):
            return ("not_listed", None)
        if len(testers) == 1:
            return ("last_tester", await _db_qdata(session, queue))
        await QueueTesterRepository().remove(
            session, queue_id=queue.id, player_id=player.id
        )
        return ("ok", await _db_qdata(session, queue))


async def leave_queue_tester(
    kit_key: str,
    uid: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[str, Optional[dict]]:
    """Odebere testera z aktivní fronty. Status ok/closed/not_listed/last_tester."""
    kit_key = str(kit_key).lower()
    uid = str(uid)
    return await _db_leave_queue_tester(
        session_factory, kit_key=kit_key, uid=uid
    )


async def _db_register_global_tester(
    session_factory, *, uid: str, ign: str
) -> bool:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_or_create_shell(
            session, discord_id=int(uid)
        )
        if await TesterRepository().is_tester(session, player_id=player.id):
            return True
        try:
            async with session.begin_nested():
                await TesterRepository().grant(
                    session, player_id=player.id, granted_by=int(uid)
                )
        except IntegrityError:
            return True
        return True


async def register_global_tester(
    uid: str,
    ign: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Zaregistruje globálně aktivního testera (joinastester). PostgreSQL.

    `testers` řádek (graceful no-op při už existujícím — unique PK).
    """
    uid = str(uid)
    return await _retry_on_integrity(
        _db_register_global_tester, session_factory=session_factory, uid=uid, ign=ign
    )


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
            status=QUEUE_ENTRY_LEFT,
            removed_at=datetime.now(timezone.utc),
            removed_reason="removeq",
        )
        return out


async def removeq(
    uid: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[dict]:
    """Vyhození hráče z fronty (/removeq). Vrací záznam hráče nebo None."""
    uid = str(uid)
    return await _db_removeq(session_factory, uid=uid)


async def _db_skip_player(session_factory, *, uid: str) -> dict:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        empty = {"channel_id": None, "kit_key": "", "was_pulled": False,
                 "removed": False, "next": None}
        if player is None:
            return empty
        pulled = await QueueEntryRepository().list_by_status(
            session, player_id=player.id, status=QUEUE_ENTRY_PULLED
        )
        waiting = await QueueEntryRepository().list_waiting_for_player(
            session, player_id=player.id
        )
        entry = pulled[0] if pulled else (waiting[0] if waiting else None)
        if entry is None:
            return empty
        channel_id = entry.room_channel_id
        kit = await session.get(Kit, entry.kit_id)
        kit_key = kit.key if kit is not None else ""
        await QueueEntryRepository().transition(
            session,
            entry_id=entry.id,
            status=QUEUE_ENTRY_LEFT,
            removed_at=datetime.now(timezone.utc),
            removed_reason="skip",
        )
        next_entry = None
        listed = await QueueEntryRepository().list_waiting(
            session, queue_id=entry.queue_id
        )
        if listed:
            n_player = await session.get(Player, listed[0].player_id)
            next_entry = _db_entry_to_dict(
                listed[0], n_player, kit.name if kit is not None else ""
            )
        return {"channel_id": channel_id, "kit_key": kit_key,
                "was_pulled": bool(pulled), "removed": True, "next": next_entry}


async def skip_player(
    uid: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict:
    """Skip hráče: vyhodí ho z roomky i z fronty (záznam → ``left``, důvod ``skip``).

    Vrací {channel_id, kit_key, was_pulled, removed, next}.
    """
    uid = str(uid)
    return await _db_skip_player(session_factory, uid=uid)


async def panel_message_id(
    kit_key: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> Optional[str]:
    """ID zprávy panelu kitu (pro update_panel) nebo None."""
    kit_key = str(kit_key).lower()
    async with db_transaction(session_factory) as session:
        kit_row = await KitRepository().get_by_name(session, kit_key)
        if kit_row is None:
            return None
        queue = await QueueRepository().get_active(session, kit_id=kit_row.id)
        if queue is None or queue.panel_message_id is None:
            return None
        return str(queue.panel_message_id)

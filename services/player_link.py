"""Propojení Discord účtu s Minecraft IGN (``/linkign``, admin ``/linkdiscord``).

Pravidla
--------
* IGN patří jinému Discord účtu → odmítnuto (ozve se admin přes ``/edituser``).
* IGN nikomu nepatří → propojí se (nový hráč, nebo změna IGN propojeného hráče).
* IGN je nepropojený záznam (typicky z ``players.json`` s historií) → hráč se
  k němu připojí. Pokud má hráč pod svým Discordem vlastní NEpropojený záznam
  (přezdívka ze serveru, ``discord-<id>``), sloučí se do záznamu s IGN:
  všechny odkazy (tester, kredity, cooldowny, výsledky, tickety…) se přesunou
  a prázdný záznam se smaže.
* Hráč, který už je propojený, se k cizímu nepropojenému záznamu s historií
  sám nepřipojí – to je rozhodnutí admina.

Sloučení běží v SAVEPOINTu: kdyby přesun narazil na duplicitu (např. aktivní
cooldown na stejný kit v obou záznamech), vrátí se celé a hráč dostane
odkaz na admina. Nic se nesloučí napůl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.base import Base, utcnow
from db.models import Player
from db.repositories.players import PLAYER_SOURCE_DISCORD, PlayerRepository
from db.repositories.sync_audit import AuditRepository
from db.services.session import transaction as db_transaction

IGN_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

LINK_CREATED = "created"
LINK_ADOPTED = "adopted"
LINK_MERGED = "merged"
LINK_RENAMED = "renamed"
LINK_UNCHANGED = "unchanged"

REFUSE_INVALID_IGN = "invalid_ign"
REFUSE_IGN_TAKEN = "ign_taken"
REFUSE_ALREADY_LINKED = "already_linked"
REFUSE_MERGE_CONFLICT = "merge_conflict"

REFUSAL_MESSAGES = {
    REFUSE_INVALID_IGN: "❌ Neplatné IGN – Minecraft jméno má 3–16 znaků (písmena, čísla, `_`).",
    REFUSE_IGN_TAKEN: (
        "❌ IGN **{ign}** už je propojené s jiným Discord účtem. Pokud je tvoje, "
        "napiš adminovi (opraví to přes `/edituser`)."
    ),
    REFUSE_ALREADY_LINKED: (
        "❌ Už jsi propojený jako **{current}** a IGN **{ign}** má v databázi "
        "vlastní historii. Převod historie musí udělat admin."
    ),
    REFUSE_MERGE_CONFLICT: (
        "❌ Tvoje záznamy nejde sloučit automaticky (oba mají stejná data, "
        "např. cooldown na stejný kit). Napiš adminovi."
    ),
}


class LinkRefused(Exception):
    def __init__(self, code: str, **fmt):
        self.code = code
        super().__init__(REFUSAL_MESSAGES[code].format(**fmt))


@dataclass(frozen=True)
class LinkOutcome:
    status: str
    ign: str
    player_id: int
    merged_from: Optional[int] = None


def _player_fk_columns():
    """Every (table, column) with a foreign key to ``players.id``."""
    players = Base.metadata.tables["players"]
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table is players and fk.column.name == "id":
                    yield table, column


async def merge_players(session: AsyncSession, *, source: Player, target: Player) -> None:
    """Move everything referencing ``source`` to ``target`` and delete ``source``.

    Raises :class:`LinkRefused` (merge_conflict) when a unique constraint
    rejects the move; the caller's SAVEPOINT rolls the partial move back.
    """
    try:
        for table, column in _player_fk_columns():
            await session.execute(
                update(table).where(column == source.id).values({column.name: target.id})
            )
        await session.delete(source)
        await session.flush()
    except IntegrityError as err:
        raise LinkRefused(REFUSE_MERGE_CONFLICT) from err


async def link_ign_in_session(
    session: AsyncSession,
    *,
    discord_id: int,
    ign: str,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
) -> LinkOutcome:
    ign_clean = (ign or "").strip()
    if not IGN_RE.match(ign_clean):
        raise LinkRefused(REFUSE_INVALID_IGN)
    repo = PlayerRepository()
    did = int(discord_id)
    now = utcnow()
    me = await repo.get_by_discord_id(session, did)
    target = await repo.get_by_ign(session, ign_clean)

    if target is not None and target.discord_id not in (None, did):
        raise LinkRefused(REFUSE_IGN_TAKEN, ign=ign_clean)

    merged_from = None
    if me is None and target is None:
        player = Player(discord_id=did, ign=ign_clean, source=PLAYER_SOURCE_DISCORD,
                        ign_linked_at=now)
        session.add(player)
        status = LINK_CREATED
    elif me is None:
        target.discord_id = did
        target.ign = ign_clean
        target.ign_linked_at = now
        player, status = target, LINK_ADOPTED
    elif target is None or target.id == me.id:
        status = LINK_UNCHANGED if (me.ign == ign_clean and me.ign_linked_at) else LINK_RENAMED
        me.ign = ign_clean
        me.ign_linked_at = me.ign_linked_at or now
        player = me
    else:
        # target je nepropojený záznam s IGN, me je jiný záznam pod mým Discordem.
        if me.ign_linked_at is not None:
            raise LinkRefused(REFUSE_ALREADY_LINKED, ign=ign_clean, current=me.ign)
        merged_from = me.id
        async with session.begin_nested():
            await merge_players(session, source=me, target=target)
            target.discord_id = did
            target.ign = ign_clean
            target.ign_linked_at = now
            await session.flush()
        player, status = target, LINK_MERGED

    await session.flush()
    if status != LINK_UNCHANGED:
        await AuditRepository().append(
            session,
            action="ign_link",
            actor_id=actor_id if actor_id is not None else did,
            actor_name=actor_name,
            entity_type="player",
            entity_id=str(player.id),
            details={"status": status, "ign": ign_clean, "discord_id": did,
                     "merged_from_player_id": merged_from},
        )
    return LinkOutcome(status=status, ign=ign_clean, player_id=player.id, merged_from=merged_from)


async def link_ign(
    discord_id: int,
    ign: str,
    *,
    session_factory,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
) -> LinkOutcome:
    """Propojí Discord účet s IGN v jedné transakci (viz pravidla v modulu)."""
    async with db_transaction(session_factory) as session:
        return await link_ign_in_session(
            session, discord_id=discord_id, ign=ign,
            actor_id=actor_id, actor_name=actor_name,
        )


async def linked_ign(discord_id: int, *, session_factory) -> Optional[str]:
    """Propojené IGN hráče, nebo ``None`` (nepropojený / neznámý)."""
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None or player.ign_linked_at is None:
            return None
        return player.ign

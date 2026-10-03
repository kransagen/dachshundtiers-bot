"""HT výsledky – čistá logika + PostgreSQL (bez discord.py).

„Unified HT result system": výsledek evaluace (z /result) se zapisuje SEM –
ať jde o HT3+ ticket, nebo klasický queue výsledek. Tady je jediný zdroj
pravdy pro výsledky, historii i propojení s hráčem a ticketem.

Výsledek je propojený s:
  - hráčem (playerId / playerName = Discord ID a jméno),
  - IGN (ign),
  - evaluátorem (evaluatorId / evaluatorName),
  - ticketem (ticketId = ID kanálu HT ticketu; None pro queue výsledky),
  - časem (timestamp + date),
  - předchozím tierem (previousTier – hodnota z `player_current_tiers`, tj.
    zrcadla potvrzeného Discordu, v okamžiku zápisu),
  - novým tierem (newTier),
  - poznámkami (notes).

Vlastnosti:
  - výsledky se validují (tier proti cíli ticketu, propojení hráč/kit,
    otevřený ticket; tester je zajištěný na úrovni cogs),
  - idempotence: jeden HT ticket = maximálně jeden výsledek. Opakované
    odeslání nic nepřepíše a vrátí stejný uložený záznam,
  - ochrana před duplicitami: queue výsledek se nezapíše, dokud má hráč
    aktivní cooldown (4 dny) PRO DANÝ KIT (cooldown vzniká až po potvrzeném
    grantu),
  - historie se nikdy nemaže – tabulka `results` je append-only (jedna větev
    na ticket / queue / ht_fight),
  - zápis výsledku jen založí řádek do `results` (`discord_pending`);
    waitlist/HT3+ cooldown, zavření HT ticketu a log ticketu se provedou až
    v `commit_after_discord_success`, tedy po potvrzeném Discord grantu role.

DRUHÝ REŽIM TU UŽ NENÍ. ``players.json`` / ``ht_results.json`` /
``cooldowns.json`` se nečtou ani nezapisují; každá funkce vyžaduje
``session_factory`` a bez něj odmítne. Dvojí zdroj pravdy by se tiš
rozcházel – například „duplicitní" queue výsledek by se v jednom režimu našel
a ve druhém ne, takže tester by dostal jinou hlášku podle toho, zda bot
viděl databázi.

Návratové dicty mají historicky JSON-ish tvar (``playerId``, ``newTier`` …),
protože je čtou cogy a view modaly. Je to jen projekce z DB řádku
(``_db_result_to_dict``), ne formát, v jakém je data uložená.

Žádná závislost na discord.py ani github_sync → snadné testy.
"""

import logging
import re
import time

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Result
from db.tier_catalog import ladder_rank
from db.repositories.cooldowns import CooldownRepository
from db.repositories.kits import (
    KitRepository,
    TesterRoomRepository,
    TierDefinitionRepository,
)
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.queues import (
    QUEUE_ENTRY_PULLED,
    QUEUE_ENTRY_WAITING,
    QueueEntryRepository,
)
from db.repositories.results import (
    ANNOUNCEMENT_PENDING,
    PROMOTION_DISCORD_PENDING,
    ResultRepository,
)
from db.repositories.tickets import TICKET_OPEN, TicketRepository
from db.repositories.tiers import MirrorRepository
from db.services.session import transaction as db_transaction
from services.tickets import (
    HT3_TIER_LADDER,
    _db_resolve_kit,
    _db_resolve_tier,
    _db_ticket_to_dict,
    _dt_to_ms,
    _ms_to_dt,
)

log = logging.getLogger("dachshundtiers")

# Povolené tiery /result mimo HT ticket (LT5 .. LT3 + eval):
# HT3 a výš se řeší výhradně přes HT3+ tickety.
RESULT_TIERS = {"LT5", "HT5", "LT4", "HT4", "LT3", "LT3E"}
# Volba „LT3 + eval" (v kódu LT3E): hráč dostane tier LT3 (stejná role)
# a navíc eval flag – viz set_eval.
EVAL_TIER = "LT3E"

# Typ výsledku v kanonické historii (tabulka `results`) je ``normal``
# (klasický /result) nebo ``ht_fight`` (HT Fight výsledek, /topresult) –
# rozlišuje se podle sloupce ``kind``, viz ``_db_result_to_dict``.
QUEUE_RESULT_PREFIX = "queue-"

IGN_RE = re.compile(r"^\w{3,16}$")
SCORE_RE = re.compile(r"^\d{1,3}[-:]\d{1,3}$")
MAX_NOTES_LEN = 500

_QUEUE_TIERS_MESSAGE = (
    "❌ Neplatný tier! V `/result` lze zadat pouze: "
    "**LT5, HT5, LT4, HT4, LT3, LT3 + eval**."
)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Validace (čisté funkce)
# ---------------------------------------------------------------------------
def normalize_tier(value: str) -> str:
    """Normalizace tieru (velikost písmen + whitespace)."""
    return (value or "").strip().upper()


def validate_result_inputs(
    ign: str, score: str, notes: str | None
) -> tuple[bool, str]:
    """Validace volného textu /result (IGN, skóre, poznámky) před zápisem."""
    if not IGN_RE.match((ign or "").strip()):
        return False, "❌ Neplatné IGN – 3–16 znaků: písmena, čísla a podtržítko."
    if not SCORE_RE.match((score or "").strip()):
        return False, "❌ Neplatné skóre – použij formát např. **5-2**."
    if len((notes or "").strip()) > MAX_NOTES_LEN:
        return False, f"❌ Poznámky jsou příliš dlouhé (max. {MAX_NOTES_LEN} znaků)."
    return True, ""


def validate_result_tier(
    tier: str,
    *,
    target_tier: str | None = None,
    current_tier: str | None = None,
) -> tuple[bool, str]:
    """Zvaliduje nový tier výsledku.

    - ``target_tier=None`` (queue výsledek) → pouze RESULT_TIERS
      (LT5..HT5..LT3+eval); HT3+ jde jedině přes HT ticket.
    - jinak (HT3+ ticket) → tier ze žebříčku, maximálně cíl ticketu
      a nikdy horší než aktuální tier hráče (retest nedegraduje).

    Vrací ``(ok, message)``.
    """
    new = normalize_tier(tier)
    if target_tier is None:
        if new not in RESULT_TIERS:
            return False, _QUEUE_TIERS_MESSAGE
        return True, ""

    if new not in HT3_TIER_LADDER:
        return (
            False,
            f"❌ Neplatný tier `{new}`. V HT3+ ticketu se zapisuje tier ze žebříčku.",
        )
    target = normalize_tier(target_tier)
    if target not in HT3_TIER_LADDER:
        return False, f"❌ Cíl ticketu `{target}` není platný tier."
    new_idx = HT3_TIER_LADDER.index(new)
    target_idx = HT3_TIER_LADDER.index(target)
    if new_idx > target_idx:
        return (
            False,
            f"❌ Nový tier `{new}` je lepší než cíl ticketu `{target}` – "
            "výsledek nesmí přesáhnout tier, o který hráč usiloval.",
        )
    cur = normalize_tier(current_tier) if current_tier else None
    if cur and cur in HT3_TIER_LADDER:
        cur_idx = HT3_TIER_LADDER.index(cur)
        if new_idx < cur_idx:
            return (
                False,
                f"❌ Nový tier `{new}` je horší než aktuální tier hráče `{cur}` – "
                "retest hráče nedegraduje.",
            )
    return True, ""


# ---------------------------------------------------------------------------
# Záznam výsledku
# ---------------------------------------------------------------------------
async def record_result(
    *,
    ticket_id=None,
    player_id: str,
    player_name: str,
    ign: str,
    evaluator_id: str,
    evaluator_name: str,
    kit: str,
    new_tier: str,
    display_tier: str,
    score: str,
    outcome: str,
    notes: str = None,
    eval_flag: bool = False,
    now: int = None,
    date: str = "",
    queue_cooldown_ms: int = 0,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict:
    """Zapíše výsledek evaluace atomicky (validace + idempotence + zápis).

    ``ticket_id`` (ID kanálu HT ticketu) → výsledek se propojí s ticketem:
    - ticket musí existovat a být otevřený,
    - hráč musí být vlastníkem ticketu, kit musí sedět,
    - tier musí projít ``validate_result_tier`` proti cíli ticketu,
    - ticket zůstává otevřený – zavře ho až potvrzený Discord grant.

    ``ticket_id=None`` → queue výsledek:
    - tier musí být z RESULT_TIERS,
    - dokud má hráč aktivní cooldown (``queue_cooldown_ms``), je odeslání
      považované za duplicitu a nepřepíše se.

    ``session_factory`` (async_sessionmaker) je povinný: zápis jde do
    PostgreSQL a result se vloží se stavem ``discord_pending``; Discord-first
    potvrzení dotáhne ``commit_promotion_with_wedge`` se stejným
    ``result_key``. Bez factory funkce odmítne (RuntimeError) – už žádný
    fallback na ``players.json`` / ``ht_results.json`` neexistuje.

    Vrací:
      - ``{"result": "created", "record": {...}, "previous_tier": ...}``
      - ``{"result": "duplicate", "existing": {...}|None}``
      - ``{"result": "not_found"}`` / ``{"result": "ticket_closed", "ticket"}``
      - ``{"result": "wrong_player", "ticket"}`` / ``{"result": "wrong_kit", "ticket"}``
      - ``{"result": "invalid_tier", "message"}``
      - ``{"result": "identity_conflict", "message"}`` (IGN patří jinému Diskordu)
    """
    return await _db_record_result(
        session_factory,
        ticket_id=ticket_id,
        player_id=player_id,
        player_name=player_name,
        ign=ign,
        evaluator_id=evaluator_id,
        evaluator_name=evaluator_name,
        kit=kit,
        new_tier=new_tier,
        display_tier=display_tier,
        score=score,
        outcome=outcome,
        notes=notes,
        eval_flag=eval_flag,
        now=now,
        date=date,
        queue_cooldown_ms=queue_cooldown_ms,
    )


# ---------------------------------------------------------------------------
# Čtení historie (restart-safe)
# ---------------------------------------------------------------------------
async def player_access_channel_ids(
    player_id, session_factory: async_sessionmaker[AsyncSession]
) -> set[int]:
    """Kanály, kde hráč může mít osobní overwrite (tester roomky, jeho tickety
    a roomky z jeho záznamů ve frontě) – jen z nich se mu po /result odebírají práva."""
    async with db_transaction(session_factory) as session:
        channels = {
            room.channel_id
            for room in await TesterRoomRepository().list_all(session)
        }
        player = await PlayerRepository().get_by_discord_id(session, int(player_id))
        if player is None:
            return channels
        for ticket in await TicketRepository().list_open(session, player_id=player.id):
            channels.add(ticket.channel_id)
        queue_entries = QueueEntryRepository()
        for status in (QUEUE_ENTRY_WAITING, QUEUE_ENTRY_PULLED):
            for entry in await queue_entries.list_by_status(
                session, player_id=player.id, status=status
            ):
                if entry.room_channel_id is not None:
                    channels.add(entry.room_channel_id)
        return channels


async def get_result_by_ticket(
    ticket_id, session_factory: async_sessionmaker[AsyncSession]
) -> dict | None:
    """Výsledek pro daný HT ticket (podle ID kanálu), nebo None."""
    if session_factory is None:
        raise RuntimeError(
            "get_result_by_ticket potřebuje PostgreSQL; ht_results.json se už nepoužívá"
        )
    return await _db_get_result_by_ticket(session_factory, ticket_id)


async def get_results_for_player(
    player_id, session_factory: async_sessionmaker[AsyncSession]
) -> list:
    """Všechny výsledky hráče v chronologickém pořadí (historie evaluací)."""
    if session_factory is None:
        raise RuntimeError(
            "get_results_for_player potřebuje PostgreSQL; ht_results.json se už nepoužívá"
        )
    return await _db_get_results_for_player(session_factory, player_id)


async def get_all_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> list:
    """Všechny výsledky (historie evaluací) v pořadí zápisu."""
    if session_factory is None:
        raise RuntimeError(
            "get_all_results potřebuje PostgreSQL; ht_results.json se už nepoužívá"
        )
    return await _db_get_all_results(session_factory)


# ---------------------------------------------------------------------------
# PostgreSQL implementace — result_key = f"result:{result_id}" tak, aby
# commit_promotion_with_wedge (cog) našel řádek po Discord mutaci.
# ---------------------------------------------------------------------------
async def _db_record_result(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ticket_id=None,
    player_id: str,
    player_name: str,
    ign: str,
    evaluator_id: str,
    evaluator_name: str,
    kit: str,
    new_tier: str,
    display_tier: str,
    score: str,
    outcome: str,
    notes: str = None,
    eval_flag: bool = False,
    now: int = None,
    date: str = "",
    queue_cooldown_ms: int = 0,
) -> dict:
    if now is None:
        now = _now_ms()
    player_id = str(player_id)
    evaluator_id = str(evaluator_id)
    stored_tier = "LT3" if eval_flag else normalize_tier(new_tier)

    async with db_transaction(session_factory) as session:
        results = ResultRepository()
        kit_row = await _db_resolve_kit(session, kit)

        # --- Idempotence / ochrana před duplicitami ---
        if ticket_id is not None:
            tid = str(ticket_id)
            existing = await results.get_by_key(session, f"result:{tid}")
            if existing is not None:
                if existing.promotion_status != PROMOTION_DISCORD_PENDING:
                    return {
                        "result": "duplicate",
                        "existing": await _db_result_to_dict(session, existing),
                    }
                # H2 audit fix: a row stuck at discord_pending (or any other
                # non-committed status) means the previous attempt's Discord
                # role grant never succeeded — commit_after_discord_success
                # (which alone would create mirror/history/cooldown rows) is
                # NEVER called unless the grant succeeds, so nothing of
                # value exists yet for this key. Treating it as "duplicate"
                # would permanently block every future retry while telling
                # the tester it already succeeded. Discard and retry clean.
                await session.delete(existing)
                await session.flush()
            result_id = tid
            kind = "ticket"
            ticket_key = f"result:{tid}"
        else:
            player = await PlayerRepository().get_by_discord_id(
                session, int(player_id)
            )
            if player is not None and queue_cooldown_ms > 0 and kit_row is not None:
                active = await CooldownRepository().get_active_waitlist(
                    session,
                    player_id=player.id,
                    kit_id=kit_row.id,
                    now=_ms_to_dt(now),
                )
                if active:
                    latest = await results.list_for_player(
                        session, player_id=player.id, limit=1
                    )
                    return {
                        "result": "duplicate",
                        "existing": (
                            await _db_result_to_dict(session, latest[0])
                            if latest
                            else None
                        ),
                    }
            result_id = f"{QUEUE_RESULT_PREFIX}{player_id}-{now}"
            if kit_row is not None and queue_cooldown_ms > 0:
                # H11 audit fix: deterministic dedup key — window = per-kit
                # cooldown aligned to ``now``. Two concurrent submissions for
                # the same player+kit in the same window land on the SAME
                # result_key; the unique ``results.result_key`` constraint then
                # rejects the loser at the DB level (handled below) instead of
                # letting both INSERT. A later retest after the cooldown
                # expires falls into a later window → new key → allowed.
                # With queue_cooldown_ms == 0 there is no defined window, so
                # the legacy time-based key is kept.
                window_ms = int(queue_cooldown_ms)
                window_start = (now // window_ms) * window_ms
                result_id = (
                    f"{QUEUE_RESULT_PREFIX}{player_id}-{kit_row.id}-{window_start}"
                )
            kind = "queue"
            ticket_key = f"result:{result_id}"
            stale = await results.get_by_key(session, ticket_key)
            if stale is not None:
                if stale.promotion_status != PROMOTION_DISCORD_PENDING:
                    return {
                        "result": "duplicate",
                        "existing": await _db_result_to_dict(session, stale),
                    }
                await session.delete(stale)
                await session.flush()

        # --- Validace ---
        if ticket_id is not None:
            tid = str(ticket_id)
            ticket = await TicketRepository().get_by_channel(session, int(tid))
            if ticket is None:
                return {"result": "not_found"}
            if ticket.status != TICKET_OPEN:
                return {
                    "result": "ticket_closed",
                    "ticket": await _db_ticket_to_dict(session, ticket),
                }
            owner = await PlayerRepository().get_by_id(session, ticket.player_id)
            owner_did = owner.discord_id if owner is not None else None
            if str(owner_did if owner_did is not None else ticket.player_id) != player_id:
                return {
                    "result": "wrong_player",
                    "ticket": await _db_ticket_to_dict(session, ticket),
                }
            if (
                kit_row is None
                or ticket.kit_id is None
                or ticket.kit_id != kit_row.id
            ):
                return {
                    "result": "wrong_kit",
                    "ticket": await _db_ticket_to_dict(session, ticket),
                }
            target_tier = (
                await TierDefinitionRepository().get_by_id(session, ticket.target_tier_id)
                if ticket.target_tier_id is not None
                else None
            )
            current_tier = (
                await TierDefinitionRepository().get_by_id(session, ticket.current_tier_id)
                if ticket.current_tier_id is not None
                else None
            )
            ok_validate, msg_validate = validate_result_tier(
                new_tier,
                target_tier=target_tier.code if target_tier else None,
                current_tier=current_tier.code if current_tier else None,
            )
            if not ok_validate:
                return {"result": "invalid_tier", "message": msg_validate}
        else:
            if kit_row is None:
                return {"result": "invalid_tier", "message": f"Neznámý kit: {kit}"}
            ok_validate, msg_validate = validate_result_tier(new_tier)
            if not ok_validate:
                return {"result": "invalid_tier", "message": msg_validate}

        # --- Identita hráče (claim; IGN patřící jinému Diskordu → konflikt) ---
        try:
            _outcome, player = await PlayerRepository().claim_discord_id(
                session, discord_id=int(player_id), ign=(ign or "").strip()
            )
        except PlayerIdentityError as exc:
            return {"result": "identity_conflict", "message": str(exc)}

        # --- Předchozí tier: zdroj pravdy = mirror (Discord-confirmed) ---
        previous = "N/A"
        mirror = await MirrorRepository().get_current(
            session, player_id=player.id, kit_id=kit_row.id
        )
        if mirror is not None:
            prev_tier = await TierDefinitionRepository().get_by_id(
                session, mirror.tier_id
            )
            if prev_tier is not None:
                previous = prev_tier.code
        if ticket_id is None:
            prev_rank = ladder_rank(previous)
            new_rank = ladder_rank(stored_tier)
            if prev_rank is not None and new_rank is not None and new_rank < prev_rank:
                return {
                    "result": "invalid_tier",
                    "message": (
                        f"❌ Nový tier `{stored_tier}` je horší než aktuální tier "
                        f"hráče `{previous}` – /result hráče nedegraduje."
                    ),
                }
        prev_tier_row = (
            await _db_resolve_tier(session, previous) if previous != "N/A" else None
        )
        new_tier_row = await _db_resolve_tier(session, stored_tier)
        evaluator = (
            await PlayerRepository().get_by_discord_id(session, int(evaluator_id))
            if evaluator_id and evaluator_id.isdigit()
            else None
        )

        # --- Historie výsledků (append-only; stav = discord_pending) ---
        insert_kwargs = dict(
            result_key=ticket_key,
            kind=kind,
            player_id=player.id,
            kit_id=kit_row.id,
            subtype=None,
            evaluator_id=evaluator.id if evaluator is not None else None,
            ticket_channel_id=int(tid) if ticket_id is not None else None,
            previous_tier_id=prev_tier_row.id if prev_tier_row is not None else None,
            new_tier_id=new_tier_row.id if new_tier_row is not None else None,
            score=score,
            outcome=outcome,
            notes=(notes or "").strip() or None,
            eval_flag=eval_flag,
            date=date,
            recorded_at=_ms_to_dt(now),
            promotion_status=PROMOTION_DISCORD_PENDING,
        )
        try:
            async with session.begin_nested():
                row = await results.insert(session, **insert_kwargs)
        except IntegrityError:
            winner = await results.get_by_key(session, ticket_key)
            if winner is None:
                raise
            return {
                "result": "duplicate",
                "existing": await _db_result_to_dict(session, winner),
            }

        record = await _db_result_to_dict(session, row)
        return {"result": "created", "record": record, "previous_tier": previous}


async def _db_result_to_dict(session: AsyncSession, row: Result) -> dict:
    """Rekonstrukce JSON tvaru záznamu z DB řádku (kompatibilní dict)."""
    player = await PlayerRepository().get_by_id(session, row.player_id)
    evaluator = (
        await PlayerRepository().get_by_id(session, row.evaluator_id)
        if row.evaluator_id is not None
        else None
    )
    kit = await KitRepository().get_by_id(session, row.kit_id)
    previous = (
        await TierDefinitionRepository().get_by_id(session, row.previous_tier_id)
        if row.previous_tier_id is not None
        else None
    )
    new_tier = (
        await TierDefinitionRepository().get_by_id(session, row.new_tier_id)
        if row.new_tier_id is not None
        else None
    )
    player_did = player.discord_id if player is not None else None
    result_id = row.result_key.removeprefix("result:").removeprefix("ht_fight:")
    record = {
        "id": result_id,
        "kind": row.kind,
        "ticketId": str(row.ticket_channel_id) if row.ticket_channel_id else None,
        "playerId": str(player_did if player_did is not None else row.player_id),
        "playerName": player.ign if player is not None else "",
        "ign": player.ign if player is not None else "",
        "evaluatorId": str(evaluator.discord_id) if evaluator is not None and evaluator.discord_id else "",
        "evaluatorName": evaluator.ign if evaluator is not None else "",
        "kit": kit.name if kit is not None else "",
        "previousTier": previous.code if previous is not None else "N/A",
        "newTier": new_tier.code if new_tier is not None else "",
        "displayTier": "LT3 + eval" if row.eval_flag else (new_tier.code if new_tier is not None else ""),
        "score": row.score or "",
        "outcome": row.outcome or "",
        "notes": (row.notes or "").strip() or None,
        "eval": bool(row.eval_flag),
        "resultType": "ht_fight" if row.kind == "ht_fight" else "normal",
        "timestamp": _dt_to_ms(row.recorded_at) or 0,
        "date": row.date or "",
    }
    if row.kind == "ht_fight":
        record["fightTier"] = normalize_tier(row.subtype) if row.subtype else ""
        record["tierStatus"] = (row.tier_status or "").strip()
        record["opponentId"] = str(row.opponent_id) if row.opponent_id else None
        record["opponentName"] = row.opponent_name or ""
        record["announcement"] = row.announcement_status or ANNOUNCEMENT_PENDING
    return record


async def _db_get_result_by_ticket(
    session_factory: async_sessionmaker[AsyncSession], ticket_id
) -> dict | None:
    async with db_transaction(session_factory) as session:
        row = await ResultRepository().get_by_key(session, f"result:{ticket_id}")
        return await _db_result_to_dict(session, row) if row is not None else None


async def _db_get_results_for_player(
    session_factory: async_sessionmaker[AsyncSession], player_id
) -> list:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(player_id))
        if player is None:
            return []
        rows = await ResultRepository().list_for_player(
            session, player_id=player.id, limit=1000
        )
        out = [await _db_result_to_dict(session, r) for r in rows]
        out.sort(key=lambda r: r.get("timestamp", 0))
        return out


async def _db_get_all_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> list:
    async with db_transaction(session_factory) as session:
        rows = await ResultRepository().list_all(session)
        return [await _db_result_to_dict(session, r) for r in rows]
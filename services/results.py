"""HT výsledky – čistá logika + transakční stav (bez discord.py).

„Unified HT result system": výsledek evaluace (z /result) se zapisuje SEM –
ať jde o HT3+ ticket, nebo klasický queue výsledek. Tady je jediný zdroj
pravdy pro výsledky, historii i propojení s hráčem a ticketem.

Výsledek je propojený s:
  - hráčem (playerId / playerName = Discord ID a jméno),
  - IGN (ign),
  - evaluátorem (evaluatorId / evaluatorName),
  - ticketem (ticketId = ID kanálu HT ticketu; None pro queue výsledky),
  - časem (timestamp + date),
  - předchozím tierem (previousTier – hodnota z players.json v čase zápisu),
  - novým tierem (newTier),
  - poznámkami (notes).

Vlastnosti:
  - výsledky se validují (tier proti cíli ticketu, propojení hráč/kit,
    otevřený ticket; tester je zajištěný na úrovni cogs),
  - idempotence: jeden HT ticket = maximálně jeden výsledek. Opakované
    odeslání nic nepřepíše a vrátí stejný uložený záznam,
  - ochrana před duplicitami: queue výsledek se nezapíše, dokud má hráč
    aktivní cooldown (4 dny),
  - historie se nikdy nemaže – data/ht_results.json je append-only
    (klíč = ticketId / synthetic queue-id, hodnoty jsou neměnné záznamy),
  - potvrzení výsledku atomicky aktualizuje kanonickou databázi hráčů
    (data/players.json) a zavírá HT ticket včetně HT3+ cooldownu
    (HT3_COOLDOWN_MS) a zápisu do logu ticketu.

Žádná závislost na discord.py ani github_sync → snadné testy.
"""

import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Result
from db.repositories.cooldowns import COOLDOWN_HT3, COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import KitRepository, TierDefinitionRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.results import (
    PROMOTION_COMMITTED,
    PROMOTION_DISCORD_PENDING,
    ResultRepository,
)
from db.repositories.sync_audit import AuditRepository
from db.repositories.tickets import TICKET_OPEN, TicketRepository
from db.repositories.tiers import MirrorRepository
from db.services.session import transaction as db_transaction
from services.player_identity import PlayerIdentityConflict, claim_ign
from services.store import read as store_read, transaction
from services.tickets import (
    HT3_COOLDOWNS_FILE,
    HT3_TIER_LADDER,
    HT_TICKETS_FILE,
    HT_TICKET_LOGS_FILE,
    STATUS_CLOSED,
    STATUS_OPEN,
    _db_resolve_kit,
    _db_resolve_tier,
    _db_ticket_to_dict,
    _dt_to_ms,
    _ms_to_dt,
)
from utils import migrate_mode_keys

log = logging.getLogger("dachshundtiers")

# Povolené tiery /result mimo HT ticket (LT5 .. LT3 + eval):
# HT3 a výš se řeší výhradně přes HT3+ tickety.
RESULT_TIERS = {"LT5", "HT5", "LT4", "HT4", "LT3", "LT3E"}
# Volba „LT3 + eval" (v kódu LT3E): hráč dostane tier LT3 (stejná role)
# do players.json a navíc status evalu (data/evals.json) – viz set_eval.
EVAL_TIER = "LT3E"

# Typ výsledku v kanonické historii (data/ht_results.json):
#   normal   – klasický /result (queue nebo HT3+ eval ticket),
#   ht_fight – HT Fight výsledek (/topresult) – zapisuje se do STEJNÉ historie;
#              výhra povyšuje hráče na další tier (players.json),
#              prohra tier nemění.
RESULT_TYPES = ("normal", "ht_fight")

# Stav odeslání oznámení výsledku do kanálu (/topresult announcement):
#   pending – záznam vznikl, oznámení se ještě neodeslalo,
#   sent    – zpráva je v kanálu (messageId),
#   failed  – odeslání selhalo (retry tlačítkem v UI).
ANNOUNCEMENT_PENDING = "pending"
ANNOUNCEMENT_SENT = "sent"
ANNOUNCEMENT_FAILED = "failed"
ANNOUNCEMENT_STATUSES = frozenset(
    {ANNOUNCEMENT_PENDING, ANNOUNCEMENT_SENT, ANNOUNCEMENT_FAILED}
)

HT_RESULTS_FILE = "ht_results.json"
QUEUE_RESULT_PREFIX = "queue-"

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
# Kanonická databáze hráčů (players.json)
# ---------------------------------------------------------------------------
def apply_result_to_players(
    players: list,
    ign: str,
    kit: str,
    new_tier: str,
    current_date: str,
    *,
    player_id: str | None = None,
) -> tuple[list, str]:
    """Aplikuje výsledek na seznam hráčů (kanonická players.json).

    Vrací ``(players, previous_tier)`` – previous_tier je hodnota z players.json
    PŘED zápisem („N/A", když hráč záznam nemá). Čistá funkce: vstupní seznam
    se nikdy nemutuje (kopie), idempotentní aplikace – stejná volání na
    stejném stavu dávají stejný výsledek.

    ``player_id`` (Discord ID) = primární identita (services/player_identity):
    hráč se najde podle Discord ID, IGN se případně přejmenuje / adopuje
    historický záznam bez Discord ID. IGN patřící JINÉMU Discord ID zvedá
    ``PlayerIdentityConflict`` (operaci odmítnout, nikdy neslučovat).
    Bez ``player_id`` = legacy chování (čistá case-insensitive shoda IGN).
    """
    players = _copy_players_from(players)
    ign_clean = (ign or "").strip()
    if player_id:
        players, player, _outcome = claim_ign(
            players, discord_id=player_id, ign=ign_clean
        )
    else:
        player = next(
            (
                p
                for p in players
                if str(p.get("username", "")).strip().lower() == ign_clean.lower()
            ),
            None,
        )
        if player is None:
            player = {"username": ign_clean, "modes": {}, "history": {}}
            players.append(player)
    previous = "N/A"
    player.setdefault("modes", {})
    player.setdefault("history", {})
    # Sjednocení názvů kitů: klíče modes/history jdou POUZE pod kanonickým
    # (display-case) názvem kitu z kits.json. Existující klíč s jiným case se
    # migruje bezeztrátově – nikdy nevzniknou dva klíče jednoho kitu
    # („MolePVP" i „molepvp").
    kit_key = migrate_mode_keys(player["modes"], kit)
    migrate_mode_keys(player["history"], kit)
    if player["modes"].get(kit_key):
        previous = str(player["modes"][kit_key]).upper()
    player["history"].setdefault(kit_key, [])
    player["modes"][kit_key] = new_tier
    player["history"][kit_key].append({"date": current_date, "tier": new_tier})
    return players, previous


def _copy_players_from(players) -> list:
    """Hluboká kopie hráčských záznamů (modes/history) – nemutuje vstup."""
    return [
        {
            **p,
            "modes": dict(p.get("modes") or {}),
            "history": {k: list(v) for k, v in (p.get("history") or {}).items()},
        }
        for p in (players or [])
    ]


# ---------------------------------------------------------------------------
# Záznam výsledku
# ---------------------------------------------------------------------------
def make_result(
    *,
    result_id: str,
    kind: str,
    ticket_id: str | None,
    player_id: str,
    player_name: str,
    ign: str,
    evaluator_id: str,
    evaluator_name: str,
    kit: str,
    previous_tier: str,
    new_tier: str,
    display_tier: str,
    score: str,
    outcome: str,
    notes: str | None,
    eval_flag: bool,
    now: int,
    date: str,
    result_type: str = "normal",
    fight_tier: str = None,
    tier_status: str = None,
    opponent_id: str = None,
    opponent_name: str = "",
) -> dict:
    """Sestaví neměnný záznam výsledku (bez zápisu).

    ``result_type`` (``normal`` | ``ht_fight``) rozlišuje klasický /result od
    HT Fight výsledku (/topresult). HT Fight záznam navíc nese ``fightTier``,
    ``tierStatus``, ``opponentId``/``opponentName`` a stav oznámení
    ``announcement`` – povýšení hráče řeší services/topresult.py.
    """
    record = {
        "id": result_id,
        "kind": kind,
        "ticketId": str(ticket_id) if ticket_id else None,
        "playerId": str(player_id),
        "playerName": player_name or "",
        "ign": (ign or "").strip(),
        "evaluatorId": str(evaluator_id),
        "evaluatorName": evaluator_name or "",
        "kit": (kit or "").strip(),
        "previousTier": previous_tier,
        "newTier": normalize_tier(new_tier),
        "displayTier": display_tier,
        "score": score or "",
        "outcome": outcome or "",
        "notes": (notes or "").strip() or None,
        "eval": bool(eval_flag),
        "resultType": result_type,
        "timestamp": now,
        "date": date,
    }
    if result_type == "ht_fight":
        record["fightTier"] = normalize_tier(fight_tier)
        record["tierStatus"] = (tier_status or "").strip()
        record["opponentId"] = str(opponent_id) if opponent_id else None
        record["opponentName"] = opponent_name or ""
        record["announcement"] = ANNOUNCEMENT_PENDING
    return record


def get_result_type(record: dict) -> str:
    """Typ výsledku; staré záznamy bez pole = ``normal``."""
    if not isinstance(record, dict):
        return "normal"
    rt = record.get("resultType")
    return rt if rt in RESULT_TYPES else "normal"


def get_result_announcement(record: dict) -> str:
    """Stav oznámení výsledku; staré záznamy bez pole = ``pending``."""
    if not isinstance(record, dict):
        return ANNOUNCEMENT_PENDING
    status = record.get("announcement")
    return status if status in ANNOUNCEMENT_STATUSES else ANNOUNCEMENT_PENDING


def close_ticket_in_tx(
    tx,
    tickets: dict,
    ticket: dict,
    ticket_id,
    *,
    now: int,
    actor_id: str,
    actor_name: str = "",
    ht3_cooldown_ms: int = 0,
    log_action: str = "result",
    log_details: str | None = None,
) -> None:
    """Uzavře HT ticket uvnitř otevřené transakce (sdílené /result a /topresult).

    Zavře ticket (status + closedAt), nastaví HT3+ cooldown vlastníkovi na
    klíč kitu a připíše událost do logu ticketu. Předpokládá, že transaction
    má v ``files`` HT_TICKETS_FILE, HT3_COOLDOWNS_FILE a HT_TICKET_LOGS_FILE.
    """
    ticket["status"] = STATUS_CLOSED
    ticket["closedAt"] = now
    tx.set(HT_TICKETS_FILE, tickets)

    if ht3_cooldown_ms > 0:
        owner_id = str(ticket.get("ownerId", ""))
        if owner_id:
            ht3_cd = tx.get(HT3_COOLDOWNS_FILE, {})
            ht3_cd.setdefault(owner_id, {})[str(ticket.get("kit", ""))] = (
                now + ht3_cooldown_ms
            )
            tx.set(HT3_COOLDOWNS_FILE, ht3_cd)

    logs = tx.get(HT_TICKET_LOGS_FILE, {})
    logs.setdefault(str(ticket_id), []).append(
        {
            "ts": now,
            "action": log_action,
            "actorId": str(actor_id),
            "actorName": actor_name or "",
            "details": log_details,
        }
    )
    tx.set(HT_TICKET_LOGS_FILE, logs)


def _latest_player_result(results: dict, player_id: str) -> dict | None:
    """Nejnovější záznam hráče v historii (podle timestamp)."""
    best = None
    for r in results.values():
        if not isinstance(r, dict):
            continue
        if str(r.get("playerId", "")) != str(player_id):
            continue
        if best is None or r.get("timestamp", 0) > best.get("timestamp", 0):
            best = r
    return best


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
    ht3_cooldown_ms: int = 0,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Zapíše výsledek evaluace atomicky (validace + idempotence + zápis).

    ``ticket_id`` (ID kanálu HT ticketu) → výsledek se propojí s ticketem:
    - ticket musí existovat a být otevřený,
    - hráč musí být vlastníkem ticketu, kit musí sedět,
    - tier musí projít ``validate_result_tier`` proti cíli ticketu,
    - ticket se po potvrzení zavře + nastaví se HT3+ cooldown + event log.

    ``ticket_id=None`` → queue výsledek:
    - tier musí být z RESULT_TIERS,
    - dokud má hráč aktivní cooldown (``queue_cooldown_ms``), je odeslání
      považované za duplicitu a nepřepíše se.

    ``session_factory`` (async_sessionmaker) → zápis do PostgreSQL (result se
    vloží se stavem ``discord_pending``; Discord-first potvrzení dotáhne
    ``commit_promotion_with_wedge`` se stejným ``result_key``).

    Vrací:
      - ``{"result": "created", "record": {...}, "previous_tier": ...}``
      - ``{"result": "duplicate", "existing": {...}|None}``
      - ``{"result": "not_found"}`` / ``{"result": "ticket_closed", "ticket"}``
      - ``{"result": "wrong_player", "ticket"}`` / ``{"result": "wrong_kit", "ticket"}``
      - ``{"result": "invalid_tier", "message"}``
      - ``{"result": "identity_conflict", "message"}`` (IGN patří jinému Diskordu)
    """
    if session_factory is not None:
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
            ht3_cooldown_ms=ht3_cooldown_ms,
        )
    if now is None:
        now = _now_ms()
    player_id = str(player_id)
    evaluator_id = str(evaluator_id)
    stored_tier = "LT3" if eval_flag else normalize_tier(new_tier)

    files = ["players.json", "cooldowns.json", HT_RESULTS_FILE]
    if ticket_id is not None:
        files += [HT_TICKETS_FILE, HT3_COOLDOWNS_FILE, HT_TICKET_LOGS_FILE]

    async def _run(tx):
        results = tx.get(HT_RESULTS_FILE, {})

        # --- Idempotence / ochrana před duplicitami ---
        if ticket_id is not None:
            tid = str(ticket_id)
            existing = results.get(tid)
            if existing is not None:
                return {"result": "duplicate", "existing": existing}
        else:
            cooldowns = tx.get("cooldowns.json", {})
            last = cooldowns.get(player_id)
            if last and queue_cooldown_ms > 0 and now - int(last) < queue_cooldown_ms:
                return {
                    "result": "duplicate",
                    "existing": _latest_player_result(results, player_id),
                }

        # --- Validace ---
        if ticket_id is not None:
            tid = str(ticket_id)
            tickets = tx.get(HT_TICKETS_FILE, {})
            ticket = tickets.get(tid)
            if ticket is None:
                return {"result": "not_found"}
            if ticket.get("status") != STATUS_OPEN:
                return {"result": "ticket_closed", "ticket": ticket}
            if str(ticket.get("ownerId", "")) != player_id:
                return {"result": "wrong_player", "ticket": ticket}
            if str(ticket.get("kit", "")).strip().lower() != str(kit).strip().lower():
                return {"result": "wrong_kit", "ticket": ticket}
            ok_validate, msg_validate = validate_result_tier(
                new_tier,
                target_tier=ticket.get("targetTier"),
                current_tier=ticket.get("currentTier"),
            )
            if not ok_validate:
                return {"result": "invalid_tier", "message": msg_validate}
            result_id = tid
            kind = "ticket"
        else:
            ok_validate, msg_validate = validate_result_tier(new_tier)
            if not ok_validate:
                return {"result": "invalid_tier", "message": msg_validate}
            result_id = f"{QUEUE_RESULT_PREFIX}{player_id}-{now}"
            kind = "queue"

        # --- Kanonická databáze hráčů (players.json) ---
        players = tx.get("players.json", [])
        try:
            players, previous = apply_result_to_players(
                players, ign, kit, stored_tier, date, player_id=player_id
            )
        except PlayerIdentityConflict as exc:
            return {"result": "identity_conflict", "message": str(exc)}
        tx.set("players.json", players)

        # --- Historie výsledků (append-only, nikdy se nemaže) ---
        result = make_result(
            result_id=result_id,
            kind=kind,
            ticket_id=ticket_id,
            player_id=player_id,
            player_name=player_name,
            ign=ign,
            evaluator_id=evaluator_id,
            evaluator_name=evaluator_name,
            kit=kit,
            previous_tier=previous,
            new_tier=new_tier,
            display_tier=display_tier,
            score=score,
            outcome=outcome,
            notes=notes,
            eval_flag=eval_flag,
            now=now,
            date=date,
        )
        results[result_id] = result
        tx.set(HT_RESULTS_FILE, results)

        # --- Cooldown hráče (queue, 4 dny) ---
        cooldowns = tx.get("cooldowns.json", {})
        cooldowns[player_id] = now
        tx.set("cooldowns.json", cooldowns)

        # --- Ticket: zavření + HT3+ cooldown + event log (atomicky) ---
        if ticket_id is not None:
            close_ticket_in_tx(
                tx,
                tickets,
                ticket,
                tid,
                now=now,
                actor_id=evaluator_id,
                actor_name=evaluator_name,
                ht3_cooldown_ms=ht3_cooldown_ms,
                log_action="result",
                log_details=f"{previous} → {normalize_tier(new_tier)}",
            )

        return {
            "result": "created",
            "record": result,
            "previous_tier": previous,
        }

    return await transaction(tuple(files), _run)


# ---------------------------------------------------------------------------
# Čtení historie (restart-safe)
# ---------------------------------------------------------------------------
async def get_result_by_ticket(
    ticket_id, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> dict | None:
    """Výsledek pro daný HT ticket (podle ID kanálu), nebo None."""
    if session_factory is not None:
        return await _db_get_result_by_ticket(session_factory, ticket_id)
    results = await store_read(HT_RESULTS_FILE, {})
    return results.get(str(ticket_id))


async def get_results_for_player(
    player_id, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> list:
    """Všechny výsledky hráče v chronologickém pořadí (historie evaluací)."""
    if session_factory is not None:
        return await _db_get_results_for_player(session_factory, player_id)
    results = await store_read(HT_RESULTS_FILE, {})
    out = [
        r
        for r in results.values()
        if isinstance(r, dict) and str(r.get("playerId", "")) == str(player_id)
    ]
    out.sort(key=lambda r: r.get("timestamp", 0))
    return out


async def get_all_results(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list:
    """Všechny výsledky (historie evaluací) v pořadí zápisu."""
    if session_factory is not None:
        return await _db_get_all_results(session_factory)
    results = await store_read(HT_RESULTS_FILE, {})
    return [r for r in results.values() if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# DB mode (PostgreSQL) — result_key = f"result:{result_id}" tak, aby
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
    ht3_cooldown_ms: int = 0,
) -> dict:
    if now is None:
        now = _now_ms()
    player_id = str(player_id)
    evaluator_id = str(evaluator_id)
    stored_tier = "LT3" if eval_flag else normalize_tier(new_tier)

    async with db_transaction(session_factory) as session:
        results = ResultRepository()

        # --- Idempotence / ochrana před duplicitami ---
        if ticket_id is not None:
            tid = str(ticket_id)
            existing = await results.get_by_key(session, f"result:{tid}")
            if existing is not None:
                if existing.promotion_status == PROMOTION_COMMITTED:
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
            if player is not None and queue_cooldown_ms > 0:
                active = await CooldownRepository().is_active(
                    session,
                    player_id=player.id,
                    cooldown_type=COOLDOWN_WAITLIST,
                    kit_id=None,
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
            kind = "queue"
            ticket_key = f"result:{result_id}"

        # --- Validace ---
        kit_row = await _db_resolve_kit(session, kit)
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
        row = await results.insert(
            session,
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

        # --- Cooldown hráče (queue, 4 dny; i ticket výsledek ho prodlužuje) ---
        if queue_cooldown_ms > 0:
            await CooldownRepository().upsert(
                session,
                player_id=player.id,
                cooldown_type=COOLDOWN_WAITLIST,
                kit_id=None,
                expires_at=_ms_to_dt(now + int(queue_cooldown_ms)),
                source="result",
            )

        # --- Ticket: zavření + HT3+ cooldown + event log (atomicky) ---
        if ticket_id is not None:
            await TicketRepository().close_by_channel(
                session, channel_id=int(tid), closed_at=_ms_to_dt(now)
            )
            if ht3_cooldown_ms > 0:
                await CooldownRepository().upsert(
                    session,
                    player_id=player.id,
                    cooldown_type=COOLDOWN_HT3,
                    kit_id=kit_row.id,
                    expires_at=_ms_to_dt(now + int(ht3_cooldown_ms)),
                    source="ticket_close",
                )
            await AuditRepository().append(
                session,
                action="result",
                actor_id=int(evaluator_id) if evaluator_id.isdigit() else None,
                actor_name=evaluator_name or "",
                entity_type="ticket",
                entity_id=str(tid),
                details={
                    "details": f"{previous} → {normalize_tier(new_tier)}",
                    "ts": int(now),
                },
            )

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
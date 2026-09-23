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

from services.store import read as store_read, transaction
from services.tickets import (
    HT3_COOLDOWNS_FILE,
    HT3_TIER_LADDER,
    HT_TICKETS_FILE,
    HT_TICKET_LOGS_FILE,
    STATUS_CLOSED,
    STATUS_OPEN,
)

log = logging.getLogger("dachshundtiers")

# Povolené tiery /result mimo HT ticket (LT5 .. LT3 + eval):
# HT3 a výš se řeší výhradně přes HT3+ tickety.
RESULT_TIERS = {"LT5", "HT5", "LT4", "HT4", "LT3", "LT3E"}
# Volba „LT3 + eval" (v kódu LT3E): hráč dostane tier LT3 (stejná role)
# do players.json a navíc status evalu (data/evals.json) – viz set_eval.
EVAL_TIER = "LT3E"

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
) -> tuple[list, str]:
    """Aplikuje výsledek na seznam hráčů (kanonická players.json).

    Vrací ``(players, previous_tier)`` – previous_tier je hodnota z players.json
    PŘED zápisem („N/A", když hráč záznam nemá). Čistá funkce: vstupní seznam
    se nikdy nemutuje (kopie), idempotentní aplikace – stejná volání na
    stejném stavu dávají stejný výsledek.
    """
    players = [
        {
            **p,
            "modes": dict(p.get("modes") or {}),
            "history": {
                k: list(v) for k, v in (p.get("history") or {}).items()
            },
        }
        for p in (players or [])
    ]
    ign_clean = (ign or "").strip()
    player = next(
        (
            p
            for p in players
            if str(p.get("username", "")).strip().lower() == ign_clean.lower()
        ),
        None,
    )
    previous = "N/A"
    if player is None:
        player = {"username": ign_clean, "modes": {}, "history": {}}
        players.append(player)
    else:
        player.setdefault("modes", {})
        player.setdefault("history", {})
        if player["modes"].get(kit):
            previous = str(player["modes"][kit]).upper()
    player["history"].setdefault(kit, [])
    player["modes"][kit] = new_tier
    player["history"][kit].append({"date": current_date, "tier": new_tier})
    return players, previous


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
) -> dict:
    """Sestaví neměnný záznam výsledku (bez zápisu)."""
    return {
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
        "timestamp": now,
        "date": date,
    }


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

    Vrací:
      - ``{"result": "created", "record": {...}, "previous_tier": ...}``
      - ``{"result": "duplicate", "existing": {...}|None}``
      - ``{"result": "not_found"}`` / ``{"result": "ticket_closed", "ticket"}``
      - ``{"result": "wrong_player", "ticket"}`` / ``{"result": "wrong_kit", "ticket"}``
      - ``{"result": "invalid_tier", "message"}``
    """
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
        players, previous = apply_result_to_players(
            players, ign, kit, stored_tier, date
        )
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
            ticket["status"] = STATUS_CLOSED
            ticket["closedAt"] = now
            tx.set(HT_TICKETS_FILE, tickets)

            if ht3_cooldown_ms > 0:
                owner_id = str(ticket.get("ownerId", ""))
                if owner_id:
                    ht3_cd = tx.get(HT3_COOLDOWNS_FILE, {})
                    ht3_cd.setdefault(owner_id, {})[
                        str(ticket.get("kit", ""))
                    ] = now + ht3_cooldown_ms
                    tx.set(HT3_COOLDOWNS_FILE, ht3_cd)

            logs = tx.get(HT_TICKET_LOGS_FILE, {})
            logs.setdefault(tid, []).append(
                {
                    "ts": now,
                    "action": "result",
                    "actorId": evaluator_id,
                    "actorName": evaluator_name or "",
                    "details": f"{previous} → {normalize_tier(new_tier)}",
                }
            )
            tx.set(HT_TICKET_LOGS_FILE, logs)

        return {
            "result": "created",
            "record": result,
            "previous_tier": previous,
        }

    return await transaction(tuple(files), _run)


# ---------------------------------------------------------------------------
# Čtení historie (restart-safe)
# ---------------------------------------------------------------------------
async def get_result_by_ticket(ticket_id) -> dict | None:
    """Výsledek pro daný HT ticket (podle ID kanálu), nebo None."""
    results = await store_read(HT_RESULTS_FILE, {})
    return results.get(str(ticket_id))


async def get_results_for_player(player_id) -> list:
    """Všechny výsledky hráče v chronologickém pořadí (historie evaluací)."""
    results = await store_read(HT_RESULTS_FILE, {})
    out = [
        r
        for r in results.values()
        if isinstance(r, dict) and str(r.get("playerId", "")) == str(player_id)
    ]
    out.sort(key=lambda r: r.get("timestamp", 0))
    return out


async def get_all_results() -> list:
    """Všechny výsledky (historie evaluací) v pořadí zápisu."""
    results = await store_read(HT_RESULTS_FILE, {})
    return [r for r in results.values() if isinstance(r, dict)]
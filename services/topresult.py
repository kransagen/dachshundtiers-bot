"""HT Fight výsledky – /topresult (čistá logika, bez discord.py).

``/topresult`` je specializovaná verze ``/result`` pro HT Fighty: vytvoří
veřejný výsledek HT Fightu v určeném kanálu a zapinguje nakonfigurovanou roli.
NENÍ to žebříček a nepočítá žádné „top" hráče.

Klíčové vlastnosti:
  - záznam jde do STEJNÉ kanonické historie výsledků jako /result
    (``data/ht_results.json``, append-only) s ``resultType: "ht_fight"`` —
    žádná samostatná databáze (žádný topresults.json),
  - **nikdy nemění tier hráče** (players.json se nedotýká): /topresult výsledek
    jen zaznamenává a publikuje, změna tieru jde vždy přes /result a HT Fight
    pravidla, která definují změnu tieru, zatím v projektu neexistují,
  - idempotence pro HT Fight ticket: klíč ``{ticketId}:ht_fight`` — druhé
    odeslání vrátí ``duplicate`` a nic nepošle dvakrát,
  - validace: HT tier ze žebříčku (bez virtuálního LT3E), skóre ``0-4``
    (``^\d+-\d+$``), status tieru neprázdný, kit registrovaný,
  - formát zprávy přesně zachovává styl používaný na serveru:

        <@HRAC> - <IGN> - **<STATUS>** - <KIT>

        **<HT_TIER> Fighty:**
        > <vyhrál|prohrál> <SKÓRE> <@SOUPER>

        <@&ROLE>

Žádná závislost na discord.py → snadné testy.
"""

import logging
import re
import time

from services.results import (
    HT_RESULTS_FILE,
    make_result,
    normalize_tier,
)
from services.store import read as store_read, transaction
from services.tickets import (
    HT3_TIER_LADDER,
    HT_TICKETS_FILE,
    STATUS_OPEN,
    is_ht_fight_ticket,
)

log = logging.getLogger("dachshundtiers")

# --- Konstanty --------------------------------------------------------------

# HT Fight tier musí být reálný tier ze žebříčku; virtuální status LT3E (eval)
# není fight tier.
HT_FIGHT_TIERS = tuple(t for t in HT3_TIER_LADDER if t != "LT3E")

# Idempotentní klíč HT Fight výsledku uvnitř ticketu: ticketId + result_type.
HT_FIGHT_TICKET_KEY_SUFFIX = ":ht_fight"
# Klíč HT Fight výsledku mimo ticket (unikatní podle času zápisu).
HT_FIGHT_RESULT_PREFIX = "htfight-"

# Skóre ve formátu používaném serverem: "0-4" (body hráče - body soupeře).
# Nepovolujeme "abc", "4", "4-", "-4" ani "4-x".
SCORE_RE = re.compile(r"^\d+-\d+$")

MAX_STATUS_LEN = 64

_MSG_BAD_SCORE = (
    "❌ Neplatné skóre `{score}`! Povolený formát je např. **0-4** "
    "(body hráče – body soupeře, bez mezer)."
)
_MSG_BAD_TIER = (
    "❌ Neplatný HT tier `{fight_tier}`. Platné HT Fight tiery: "
    + ", ".join(f"**{t}" for t in HT_FIGHT_TIERS)
    + "."
)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Validace (čisté funkce)
# ---------------------------------------------------------------------------
def validate_ht_fight_score(score: str) -> tuple[bool, str]:
    """Validace skóre HT Fightu: ``0-4`` (dvě čísla spojená pomlčkou)."""
    raw = (score or "").strip()
    if not SCORE_RE.match(raw):
        return False, _MSG_BAD_SCORE.format(score=(score or "").strip())
    return True, ""


def validate_ht_fight_tier(fight_tier: str) -> tuple[bool, str]:
    """Validace HT Fight tieru: reálný tier ze žebříčku (bez LT3E)."""
    t = normalize_tier(fight_tier)
    if t not in HT_FIGHT_TIERS:
        return False, _MSG_BAD_TIER.format(fight_tier=(fight_tier or "").strip())
    return True, ""


def validate_ht_fight_status(tier_status: str) -> tuple[bool, str]:
    """Status tieru („Zůstává Low Tier 3", …) – neprázdný a krátký."""
    s = (tier_status or "").strip()
    if not s or len(s) > MAX_STATUS_LEN:
        return (
            False,
            "❌ Neplatný status tieru – zadej krátký text, např. "
            "**Zůstává Low Tier 3** (max. 64 znaků).",
        )
    return True, ""


def validate_ht_fight_outcome(outcome: str) -> tuple[bool, str]:
    """Výsledek hráče: Won (hráč vyhrál) / Lost (hráč prohrál)."""
    o = (outcome or "").strip().upper()
    if o not in ("WON", "LOST"):
        return False, "❌ Neplatný výsledek zápasu – použij **vyhrál** nebo **prohrál**."
    return True, ""


def is_registered_kit(kit: str, kits) -> bool:
    """Je kit v registrovaném seznamu (case-insensitive)?"""
    key = (kit or "").strip().lower()
    if not key:
        return False
    return any(str(k).strip().lower() == key for k in (kits or []))


def validate_topresult_config(channel_id, role_id) -> tuple[bool, str]:
    """Ověří konfiguraci /topresult (kanál + role). 0/missing = chyba."""
    if not channel_id or not role_id:
        return (
            False,
            "❌ Chybí konfigurace **/topresult**: nastav `TOP_RESULT_CHANNEL_ID` "
            "a `TOP_RESULT_ROLE_ID` v .env (viz `.env.example`).",
        )
    return True, ""


def ht_fight_outcome_display(outcome: str) -> str:
    """Displejové sloveso výsledku: Won → „vyhrál", Lost → „prohrál"."""
    o = (outcome or "").strip().upper()
    return {"WON": "vyhrál", "LOST": "prohrál"}.get(o, (outcome or "").strip())


# ---------------------------------------------------------------------------
# Formát zprávy (přesně zachovává styl serveru)
# ---------------------------------------------------------------------------
def format_topresult_message(
    *,
    player_id,
    ign: str,
    tier_status: str,
    kit: str,
    fight_tier: str,
    outcome: str,
    score: str,
    opponent_id,
    role_id,
) -> str:
    """Sestaví text HT Fight výsledku (včetně role mentionu ``<@&id>``).

    Příklad:

        <@1419031701920940163> - mendu__ - **Zůstává Low Tier 3** - MolePVP

        **HT3 Fighty:**
        > prohrál 0-4 <@1018169843347882076>

        <@&1523984977371594772>
    """
    return "\n".join(
        [
            f"<@{player_id}> - {ign} - **{tier_status}** - {kit}",
            "",
            f"**{normalize_tier(fight_tier)} Fighty:**",
            f"> {ht_fight_outcome_display(outcome)} {score} <@{opponent_id}>",
            "",
            f"<@&{role_id}>",
        ]
    )


# ---------------------------------------------------------------------------
# Čtení aktuálního tieru hráče (jen pro kontext záznamu, nikdy se nemění)
# ---------------------------------------------------------------------------
def find_player_tier_in(players, ign: str, kit: str) -> str | None:
    """Tier hráče (IGN+kit) v daném seznamu hráčů, nebo None."""
    if not isinstance(players, list):
        return None
    ign_key = (ign or "").strip().lower()
    for p in players:
        if not isinstance(p, dict):
            continue
        if str(p.get("username", "")).strip().lower() != ign_key:
            continue
        modes = p.get("modes")
        if not isinstance(modes, dict):
            return None
        tier = modes.get(kit)
        return normalize_tier(tier) if tier else None
    return None


# ---------------------------------------------------------------------------
# Záznam HT Fight výsledku (transakčně, do kanonické historie)
# ---------------------------------------------------------------------------
async def record_ht_fight(
    *,
    ticket_id=None,
    player_id: str,
    player_name: str = "",
    ign: str,
    evaluator_id: str,
    evaluator_name: str = "",
    kit: str,
    fight_tier: str,
    score: str,
    outcome: str,
    opponent_id: str,
    opponent_name: str = "",
    tier_status: str,
    notes: str = None,
    now: int = None,
    date: str = "",
) -> dict:
    """Zapíše HT Fight výsledek atomicky do ``data/ht_results.json``.

    Na rozdíl od ``record_result`` (z /result) se při /topresult **nikdy**
    nemění players.json, cooldowny ani se nezavírá ticket – jde čistě o záznam
    + publikaci. Tier změna HT Fightu by musela projít kanonickou logikou
    /result (v projektu zatím neexistuje, viz README).

    ``ticket_id`` (ID kanálu HT Fight ticketu) → výsledek se propojí
    s ticketem a idempotentně klíčuje jako ``{ticketId}:ht_fight``:
    - ticket musí existovat, být otevřený a **typem** HT Fight ticket,
    - hráč musí být vlastníkem, kit musí sedět,
    - druhé odeslání vrátí ``duplicate`` s existujícím záznamem.

    ``ticket_id=None`` → volný HT Fight výsledek (klíč ``htfight-{hráč}-{čas}``),
    žádná deduplikace (každý zápas je samostatný).

    Vrací:
      - ``{"result": "created", "record": {...}, "previous_tier": ...}``
      - ``{"result": "duplicate", "existing": {...}}``
      - ``{"result": "not_found"}`` / ``{"result": "not_fight_ticket", "ticket"}``
      - ``{"result": "ticket_closed", "ticket"}``
      - ``{"result": "wrong_player", "ticket"}`` / ``{"result": "wrong_kit", "ticket"}``
      - ``{"result": "invalid_*", "message": ...}``
    """
    if now is None:
        now = _now_ms()
    player_id = str(player_id)
    evaluator_id = str(evaluator_id)
    score_clean = (score or "").strip()
    status_clean = (tier_status or "").strip()
    outcome_clean = "Won" if (outcome or "").strip().upper() == "WON" else "Lost"

    # Základní validace (rychlá, před transakcí) ---------------------------------
    if not player_id or not (ign or "").strip() or not (kit or "").strip():
        return {
            "result": "invalid_argument",
            "message": "❌ Hráč, IGN a kit jsou povinné.",
        }
    ok, msg = validate_ht_fight_tier(fight_tier)
    if not ok:
        return {"result": "invalid_tier", "message": msg}
    ok, msg = validate_ht_fight_score(score_clean)
    if not ok:
        return {"result": "invalid_score", "message": msg}
    ok, msg = validate_ht_fight_outcome(outcome)
    if not ok:
        return {"result": "invalid_outcome", "message": msg}
    ok, msg = validate_ht_fight_status(status_clean)
    if not ok:
        return {"result": "invalid_status", "message": msg}

    files = [HT_RESULTS_FILE, "players.json"]
    if ticket_id is not None:
        files.append(HT_TICKETS_FILE)

    async def _run(tx):
        results = tx.get(HT_RESULTS_FILE, {})

        if ticket_id is not None:
            tid = str(ticket_id)
            key = f"{tid}{HT_FIGHT_TICKET_KEY_SUFFIX}"
            existing = results.get(key)
            if existing is not None:
                return {"result": "duplicate", "existing": existing}

            tickets = tx.get(HT_TICKETS_FILE, {})
            ticket = tickets.get(tid)
            if ticket is None:
                return {"result": "not_found"}
            if not is_ht_fight_ticket(ticket):
                return {"result": "not_fight_ticket", "ticket": ticket}
            if ticket.get("status") != STATUS_OPEN:
                return {"result": "ticket_closed", "ticket": ticket}
            if str(ticket.get("ownerId", "")) != player_id:
                return {"result": "wrong_player", "ticket": ticket}
            if str(ticket.get("kit", "")).strip().lower() != str(kit).strip().lower():
                return {"result": "wrong_kit", "ticket": ticket}
            result_id = key
        else:
            result_id = f"{HT_FIGHT_RESULT_PREFIX}{player_id}-{now}"

        # Tier hráče v čase zápasu – JEN kontext záznamu (players.json se
        # nezmění – /topresult žádnou změnu tieru nedělá).
        players = tx.get("players.json", [])
        previous = find_player_tier_in(players, ign, kit) or "N/A"

        record = make_result(
            result_id=result_id,
            kind="ht_fight",
            ticket_id=str(ticket_id) if ticket_id is not None else None,
            player_id=player_id,
            player_name=player_name,
            ign=ign,
            evaluator_id=evaluator_id,
            evaluator_name=evaluator_name,
            kit=kit,
            previous_tier=previous,
            new_tier="",  # žádná změna tieru
            display_tier="",
            score=score_clean,
            outcome=outcome_clean,
            notes=notes,
            eval_flag=False,
            now=now,
            date=date,
            result_type="ht_fight",
            fight_tier=fight_tier,
            tier_status=status_clean,
            opponent_id=str(opponent_id) if opponent_id else None,
            opponent_name=opponent_name,
        )
        results[result_id] = record
        tx.set(HT_RESULTS_FILE, results)

        return {"result": "created", "record": record, "previous_tier": previous}

    return await transaction(tuple(files), _run)


# ---------------------------------------------------------------------------
# Čtení HT Fight výsledků (restart-safe)
# ---------------------------------------------------------------------------
async def get_ht_fight_result_for_ticket(ticket_id) -> dict | None:
    """HT Fight výsledek pro daný ticket (podle ID kanálu), nebo None."""
    results = await store_read(HT_RESULTS_FILE, {})
    return results.get(f"{str(ticket_id)}{HT_FIGHT_TICKET_KEY_SUFFIX}")


async def get_ht_fight_results() -> list:
    """Všechny HT Fight výsledky v pořadí zápisu."""
    results = await store_read(HT_RESULTS_FILE, {})
    return [
        r
        for r in results.values()
        if isinstance(r, dict) and r.get("resultType") == "ht_fight"
    ]
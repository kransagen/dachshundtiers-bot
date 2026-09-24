"""HT Fight výsledky – /topresult (čistá logika, bez discord.py).

``/topresult`` je specializovaná verze ``/result`` pro HT Fighty: vytvoří
veřejný výsledek HT Fightu v určeném kanálu a zapinguje nakonfigurovanou roli.
NENÍ to žebříček a nepočítá žádné „top" hráče.

Klíčové vlastnosti:
  - záznam jde do STEJNÉ kanonické historie výsledků jako /result
    (``data/ht_results.json``, append-only) s ``resultType: "ht_fight"`` —
    žádná samostatná databáze (žádný topresults.json),
  - **výhra povyšuje hráče** na další tier v players.json (canonické pravidlo:
    ``next_ticket_tier`` ze žebříčku bez virtuálního LT3E), **prohra tier
    nemění**; neznámý/nečitelný tier → žádné povýšení (nikdy se nehádá),
  - **bridge** (volitelný, jen při výhře): tester může hráče povýšit
    PŘESKOČENÍM mezistupňů rovnou na zadaný cílový tier (např. topresult o
    získání HT3, ale hráč z LT3 bridgne rovnou na LT2) – cíl musí být reálný
    tier ze žebříčku a **strictly vyšší** než aktuální tier hráče; do záznamu
    se připíše ``bridgeTier`` (auditní stopa),
  - prohra uvnitř HT Fight ticketu ticket zavře + nastaví HT3+ cooldown
    vlastníkovi a připíše událost do logu ticketu (sdílené zavírání s /result);
    výhra ticket NEZAVÍRÁ ani cooldown nenastavuje,
  - idempotence pro HT Fight ticket: klíč ``{ticketId}:ht_fight`` — druhé
    odeslání vrátí ``duplicate`` a nic nepošle dvakrát,
  - validace: HT tier ze žebříčku (bez virtuálního LT3E), skóre ``0-4``
    (``^\\d+-\\d+$``), status tieru neprázdný, kit registrovaný,
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

from services.player_identity import PlayerIdentityConflict
from services.results import (
    ANNOUNCEMENT_STATUSES,
    HT_RESULTS_FILE,
    apply_result_to_players,
    close_ticket_in_tx,
    make_result,
    normalize_tier,
)
from services.store import read as store_read, transaction
from services.tickets import (
    HT3_COOLDOWNS_FILE,
    HT3_TIER_LADDER,
    HT_TICKETS_FILE,
    HT_TICKET_LOGS_FILE,
    STATUS_OPEN,
    is_ht_fight_ticket,
    next_ticket_tier,
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

# Ochrana před duplicitou VOLNÝCH HT Fight výsledků: identický záznam
# (stejný hráč + kit + fight tier + status + skóre + outcome + soupeř) v tomto
# okně se považuje za duplicitní odeslání (dvojklik / dvakrát odeslaný zápas)
# a NEPOŠLE se dvakrát. Každý jiný zápas (jiné skóre/soupeř/status) projde.
HT_FIGHT_DEDUP_WINDOW_MS = 2 * 60 * 60 * 1000  # 2 hodiny

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
_MSG_BAD_BRIDGE = (
    "❌ Neplatný bridge tier `{bridge}`. Bridge je reálný tier ze žebříčku, "
    "na který hráč postoupí PŘESKOČENÍM mezistupňů (např. z LT3 rovnou na "
    "LT2). Platné tiery: "
    + ", ".join(f"**{t}" for t in HT_FIGHT_TIERS)
    + "."
)
_MSG_BRIDGE_ONLY_ON_WIN = (
    "❌ Bridge se zadává jen při **výhře** – při prohře hráč nepostupuje "
    "a žádný bridge tier se nepoužije."
)
_MSG_BRIDGE_NOT_HIGHER = (
    "❌ Bridge tier `{bridge}` není **vyšší** než aktuální tier hráče "
    "`{current}` – bridge slouží k povýšení přeskokem mezistupňů (např. "
    "hráč z LT3 bridgne rovnou na LT2), ne k setrvání nebo degradaci."
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _find_recent_ht_fight_duplicate(
    results: dict,
    player_id: str,
    *,
    kit: str,
    fight_tier: str,
    score: str,
    outcome: str,
    opponent_id,
    tier_status: str,
    now: int,
):
    """Volný HT Fight záznam identický s tímto odesláním (dedup), nebo None.

    Porovnává POUZE volné záznamy (žádný ticketId – ticket režim má vlastní
    idempotenci podle klíče) v časovém okně ``HT_FIGHT_DEDUP_WINDOW_MS``.
    Vrací existující záznam, jinak ``None``.
    """
    kit_low = (kit or "").strip().lower()
    ft = (fight_tier or "").strip().upper()
    sc = (score or "").strip().lower()
    oc = (outcome or "").strip()
    op = str(opponent_id or "").strip()
    ts = (tier_status or "").strip().upper()
    for record in results.values():
        if not isinstance(record, dict):
            continue
        if record.get("resultType") != "ht_fight":
            continue
        if record.get("ticketId"):
            continue
        if str(record.get("playerId", "")).strip() != str(player_id).strip():
            continue
        stamp = record.get("timestamp") or 0
        if not stamp or now - int(stamp) > HT_FIGHT_DEDUP_WINDOW_MS:
            continue
        if str(record.get("kit", "")).strip().lower() != kit_low:
            continue
        if str(record.get("fightTier", "")).strip().upper() != ft:
            continue
        if str(record.get("score", "")).strip().lower() != sc:
            continue
        if str(record.get("outcome", "")).strip() != oc:
            continue
        if str(record.get("opponentId", "") or "").strip() != op:
            continue
        if str(record.get("tierStatus", "")).strip().upper() != ts:
            continue
        return record
    return None


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


def validate_ht_fight_bridge(bridge: str) -> tuple[bool, str]:
    """Bridge tier pro /topresult: reálný tier ze žebříčku (bez LT3E).

    Bridge je cíl povýšení PŘESKOČENÍM mezistupňů (např. z LT3 rovnou na
    LT2). Samotná podmínka „bridguje se jen NA VYŠŠÍ tier, než je aktuální"
    se kontroluje až s kontextem hráče v ``record_ht_fight``.
    """
    b = normalize_tier(bridge)
    if b not in HT_FIGHT_TIERS:
        return False, _MSG_BAD_BRIDGE.format(bridge=(bridge or "").strip())
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
    previous_tier: str = "",
    new_tier: str = "",
    role_id,
) -> str:
    """Sestaví text HT Fight výsledku (včetně role mentionu ``<@&id>``).

    Řádek povýšení ``**Postup: prev → new**`` se přidá jen když hráč
    postoupil (``new_tier`` je neprázdné a různé od ``previous_tier``).

    Příklad:

        <@1419031701920940163> - mendu__ - **Povýšen na HT3** - MolePVP

        **HT3 Fighty:**
        > vyhrál 4-1 <@1018169843347882076>

        **Postup: LT3 → HT3**

        <@&1523984977371594772>
    """
    parts = [
        f"<@{player_id}> - {ign} - **{tier_status}** - {kit}",
        "",
        f"**{normalize_tier(fight_tier)} Fighty:**",
        f"> {ht_fight_outcome_display(outcome)} {score} <@{opponent_id}>",
    ]
    if new_tier and normalize_tier(new_tier) != normalize_tier(previous_tier):
        parts += [
            "",
            f"**Postup: {normalize_tier(previous_tier)} → {normalize_tier(new_tier)}**",
        ]
    parts += ["", f"<@&{role_id}>"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Čtení aktuálního tieru hráče (kontext záznamu + základ povýšení)
# ---------------------------------------------------------------------------
def find_player_tier_in(players, ign: str, kit: str, discord_id=None) -> str | None:
    """Tier hráče (kit) v daném seznamu; Discord ID má přednost před IGN."""

    def _tier_of(player):
        if not isinstance(player, dict):
            return None
        modes = player.get("modes")
        if not isinstance(modes, dict):
            return None
        tier = modes.get(kit)
        return normalize_tier(tier) if tier else None

    if not isinstance(players, list):
        return None
    if discord_id:
        did = str(discord_id)
        for p in players:
            if isinstance(p, dict) and str(p.get("discordId") or "") == did:
                return _tier_of(p)
    ign_key = (ign or "").strip().lower()
    for p in players:
        if not isinstance(p, dict):
            continue
        if str(p.get("username", "")).strip().lower() != ign_key:
            continue
        return _tier_of(p)
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
    bridge: str = None,
    notes: str = None,
    now: int = None,
    date: str = "",
    ht3_cooldown_ms: int = 0,
) -> dict:
    """Zapíše HT Fight výsledek atomicky do ``data/ht_results.json``.

    Povýšení: **výhra** posune hráče na další tier v players.json
    (``next_ticket_tier`` – žebříček bez virtuálního LT3E), **prohra** tier
    nemění. Neznámý/nečitelný aktuální tier → žádné povýšení (nikdy se nehádá).
    Prohra uvnitř HT Fight ticketu ticket zavře + nastaví HT3+ cooldown
    vlastníkovi (``ht3_cooldown_ms``) a připíše událost do logu ticketu;
    výhra ticket NEZAVÍRÁ a cooldown nenastavuje (hráč ve výhře pokračuje).

    **Bridge** (``bridge``, volitelné): tester může při výhře hráče povýšit
    PŘESKOČENÍM mezistupňů přímo na zadaný cílový tier (např. topresult o
    získání HT3, ale hráč z LT3 bridgne rovnou na LT2). Pravidla:
      - zadává se JEN při výhře (prohra → ``invalid_bridge``),
      - cíl musí být reálný tier ze žebříčku (bez LT3E) – ``invalid_bridge``,
      - když je aktuální tier hráče známý, musí být bridge STRICTLY VYŠŠÍ
        (jinak ``invalid_bridge`` – bridge není setrvání ani degradace),
      - neznámý aktuální tier (nový hráč) → bridge se akceptuje (explicitní
        záměr testera, ne hádání),
      - do záznamu se připíše ``bridgeTier`` (auditní stopa: povýšení přes
        bridge, ne standardní krok).

    ``ticket_id`` (ID kanálu HT Fight ticketu) → výsledek se propojí
    s ticketem a idempotentně klíčuje jako ``{ticketId}:ht_fight``:
    - ticket musí existovat, být otevřený a **typem** HT Fight ticket,
    - hráč musí být vlastníkem, kit musí sedět,
    - druhé odeslání vrátí ``duplicate`` s existujícím záznamem.

    ``ticket_id=None`` → volný HT Fight výsledek (klíč ``htfight-{hráč}-{čas}``);
    identické opakované odeslání ve volném režimu se dedupuje přes
    ``_find_recent_ht_fight_duplicate`` (2h okno, fingerprint zápasu).

    Vrací:
      - ``{"result": "created", "record": {...}, "previous_tier": ...}``
      - ``{"result": "duplicate", "existing": {...}}``
      - ``{"result": "identity_conflict", "message"}`` (IGN patří jinému Diskordu)
      - ``{"result": "not_found"}`` / ``{"result": "not_fight_ticket", "ticket"}``
      - ``{"result": "ticket_closed", "ticket"}``
      - ``{"result": "wrong_player", "ticket"}`` / ``{"result": "wrong_kit", "ticket"}``
      - ``{"result": "invalid_*", "message": ...}`` (včetně ``invalid_bridge``)
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

    # Bridge (volitelné): jen při výhře + reálný tier ze žebříčku.
    # Podmínka „vyšší než aktuální tier hráče" se ověří a transakci
    # (tam je znám aktuální tier z players.json).
    bridge_clean = normalize_tier(bridge) if (bridge or "").strip() else ""
    if bridge_clean:
        if outcome_clean == "Lost":
            return {"result": "invalid_bridge", "message": _MSG_BRIDGE_ONLY_ON_WIN}
        ok, msg = validate_ht_fight_bridge(bridge_clean)
        if not ok:
            return {"result": "invalid_bridge", "message": msg}

    files = [HT_RESULTS_FILE, "players.json"]
    if ticket_id is not None:
        files += [HT_TICKETS_FILE, HT3_COOLDOWNS_FILE, HT_TICKET_LOGS_FILE]

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
            # Volný HT Fight výsledek: ochrana před duplicitou (dvojklik /
            # dvakrát odeslaný identický zápas) – viz _find_recent_ht_fight_duplicate.
            dup = _find_recent_ht_fight_duplicate(
                results,
                player_id,
                kit=kit,
                fight_tier=fight_tier,
                score=score_clean,
                outcome=outcome_clean,
                opponent_id=opponent_id,
                tier_status=status_clean,
                now=now,
            )
            if dup is not None:
                return {"result": "duplicate", "existing": dup}
            result_id = f"{HT_FIGHT_RESULT_PREFIX}{player_id}-{now}"

        players = tx.get("players.json", [])
        current = find_player_tier_in(players, ign, kit, discord_id=player_id)
        previous = current or "N/A"

        promoted = None
        if outcome_clean == "Won" and bridge_clean:
            # Bridge: povýšení přeskočením na zadaný cílový tier. Cíl MUSÍ
            # být strictly vyšší než aktuální tier hráče (když je známý) –
            # jinak by „bridge" bridgoval dolů / do setrvání (chyba).
            if current and current in HT3_TIER_LADDER and (
                HT3_TIER_LADDER.index(bridge_clean)
                <= HT3_TIER_LADDER.index(current)
            ):
                return {
                    "result": "invalid_bridge",
                    "message": _MSG_BRIDGE_NOT_HIGHER.format(
                        bridge=bridge_clean, current=current
                    ),
                }
            promoted = bridge_clean
        elif outcome_clean == "Won" and current:
            promoted = next_ticket_tier(current)
        new_tier = promoted if promoted else ""

        if outcome_clean == "Won" and promoted is not None:
            try:
                players, _ = apply_result_to_players(
                    players, ign, kit, promoted, date, player_id=player_id
                )
            except PlayerIdentityConflict as exc:
                return {"result": "identity_conflict", "message": str(exc)}
            tx.set("players.json", players)

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
            new_tier=new_tier,
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
        if bridge_clean:
            record["bridgeTier"] = bridge_clean
        results[result_id] = record
        tx.set(HT_RESULTS_FILE, results)

        # Prohra uvnitř HT Fight ticketu = vyřešený ticket → zavření + HT3+
        # cooldown vlastníka + událost v logu (sdílené zavírání s /result).
        # Výhra ticket NIKDY nezavírá a cooldown nenastavuje – hráč může
        # ve výhře pokračovat, prohra ho pošle do cooldownu.
        if ticket_id is not None and outcome_clean == "Lost":
            close_ticket_in_tx(
                tx,
                tickets,
                ticket,
                tid,
                now=now,
                actor_id=evaluator_id,
                actor_name=evaluator_name,
                ht3_cooldown_ms=ht3_cooldown_ms,
                log_action="ht_fight",
                log_details=f"{previous} (prohra)",
            )

        return {"result": "created", "record": record, "previous_tier": previous}

    return await transaction(tuple(files), _run)


# ---------------------------------------------------------------------------
# Oznámení výsledku do kanálu (pending → sent/failed + messageId)
# ---------------------------------------------------------------------------
async def set_ht_fight_announcement(
    result_id: str, status: str, message_id=None
) -> dict:
    """Aktualizuje stav oznámení HT Fight výsledku (``sent``/``failed``).

    Stav se mění NA MÍSTĚ v záznamu (operativní pole, ne historie) – historie
    zůstává append-only. ``message_id`` = ID odeslané zprávy v kanálu.
    Vrací ``{"result": "ok", "record"}``, ``{"result": "not_found"}`` nebo
    ``{"result": "invalid_status"}``.
    """
    status = (status or "").strip()
    if status not in ANNOUNCEMENT_STATUSES:
        return {"result": "invalid_status"}

    async def _run(tx):
        results = tx.get(HT_RESULTS_FILE, {})
        record = results.get(str(result_id))
        if record is None:
            return {"result": "not_found"}
        record["announcement"] = status
        if message_id is not None:
            record["messageId"] = str(message_id)
        tx.set(HT_RESULTS_FILE, results)
        return {"result": "ok", "record": record}

    return await transaction((HT_RESULTS_FILE,), _run)


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
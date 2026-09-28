"""HT Fight výsledky – /topresult (čistá logika, bez discord.py).

``/topresult`` je specializovaná verze ``/result`` pro HT Fighty: vytvoří
veřejný výsledek HT Fightu v určeném kanálu a zapinguje nakonfigurovanou roli.
NENÍ to žebříček a nepočítá žádné „top" hráče.

Klíčové vlastnosti:
  - záznam jde do STEJNÉ kanonické historie výsledků jako /result (tabulka
    ``results``, append-only) s ``resultType: "ht_fight"`` — žádná samostatná
    databáze (žádný topresults.json),
  - **výhra povyšuje hráče** na další tier (canonické pravidlo:
    ``next_ticket_tier`` ze žebříčku bez virtuálního LT3E) — povýšení se
    potvrdí na Discordu a zapíše do ``player_current_tiers``,
    **prohra tier nemění**; neznámý/nečitelný tier → žádné povýšení (nikdy
    se nehádá, mirror je jediný zdroj aktuálního tieru),
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

DRUHÝ REŽIM TU UŽ NENÍ. ``players.json`` / ``ht_results.json`` /
``ht_tickets.json`` / ``ht3_cooldowns.json`` se nečtou ani nezapisují;
``record_ht_fight`` a čtecí funkce vyžadují ``session_factory``.

Žádná závislost na discord.py → snadné testy.
"""

import logging
import re
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.repositories.cooldowns import COOLDOWN_HT3, CooldownRepository
from db.repositories.kits import KitRepository, TierDefinitionRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.results import (
    ANNOUNCEMENT_STATUSES,
    PROMOTION_COMMITTED,
    PROMOTION_DISCORD_PENDING,
    ResultRepository,
)
from db.repositories.sync_audit import AuditRepository
from db.repositories.tickets import TICKET_OPEN, TicketRepository
from db.repositories.tiers import MirrorRepository
from db.services.session import transaction as db_transaction
from services.results import (
    _db_result_to_dict,
    normalize_tier,
)
from services.tickets import (
    HT3_TIER_LADDER,
    TICKET_TYPE_FIGHT,
    _db_resolve_kit,
    _db_resolve_tier,
    _db_ticket_to_dict,
    _ms_to_dt,
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
    session_factory: async_sessionmaker[AsyncSession],
) -> dict:
    """Zapíše HT Fight výsledek atomicky do kanonické historie výsledků.

    Povýšení: **výhra** posune hráče na další tier (``next_ticket_tier`` –
    žebříček bez virtuálního LT3E), **prohra** tier nemění. Neznámý aktuální
    tier → žádné povýšení (nikdy se nehádá, mirror je jediný zdroj aktuálního
    tieru). Prohra uvnitř HT Fight ticketu ticket zavře + nastaví HT3+
    cooldown vlastníkovi (``ht3_cooldown_ms``); výhra ticket NEZAVÍRÁ.

    **Bridge** (``bridge``, volitelné): tester může při výhře hráče povýšit
    PŘESKOČENÍM přímo na zadaný cílový tier. Zadává se JEN při výhře a cíl
    musí být reálný tier ze žebříčku **strictly vyšší** než aktuální; neznámý
    aktuální tier → bridge se akceptuje (explicitní záměr, ne hádání). Povýšení
    se připíše jako ``bridgeTier`` (auditní stopa).

    ``ticket_id`` → propojí výsledek s ticketem, idempotentně klíčuje jako
    ``ht_fight:{ticketId}:ht_fight``; ticket musí existovat, být otevřený a
    TYPEM HT Fight. ``ticket_id=None`` → volný výsledek
    (``ht_fight:htfight-{hráč}-{čas}``); identické opakované odeslání se
    dedupuje (2h okno).

    ``session_factory`` (povinný) → zápis do PostgreSQL (HT Fight tier jde do
    ``Result.subtype``; výhra se vloží se stavem ``discord_pending`` – po
    potvrzení Discordu ho cog dotáhne ``commit_promotion_with_wedge`` se
    stejným ``result_key``, prohra zůstane bez promotion stavu).

    Vrací:
      - ``{"result": "created", "record": {...}, "previous_tier": ...}``
      - ``{"result": "duplicate", "existing": {...}}``
      - ``{"result": "identity_conflict", "message"}``
      - ``{"result": "not_found"}`` / ``{"result": "not_fight_ticket", "ticket"}``
      - ``{"result": "ticket_closed", "ticket"}``
      - ``{"result": "wrong_player", "ticket"}`` / ``{"result": "wrong_kit", "ticket"}``
      - ``{"result": "invalid_*", "message": ...}`` (včetně ``invalid_bridge``)
    """
    return await _db_record_ht_fight(
        session_factory,
        ticket_id=ticket_id,
        player_id=player_id,
        player_name=player_name,
        ign=ign,
        evaluator_id=evaluator_id,
        evaluator_name=evaluator_name,
        kit=kit,
        fight_tier=fight_tier,
        score=score,
        outcome=outcome,
        opponent_id=opponent_id,
        opponent_name=opponent_name,
        tier_status=tier_status,
        bridge=bridge,
        notes=notes,
        now=now,
        date=date,
        ht3_cooldown_ms=ht3_cooldown_ms,
    )


# ---------------------------------------------------------------------------
# Oznámení výsledku do kanálu (pending → sent/failed + messageId)
# ---------------------------------------------------------------------------
async def set_ht_fight_announcement(
    result_id: str,
    status: str,
    message_id=None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
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
    if session_factory is None:
        raise RuntimeError(
            "set_ht_fight_announcement potřebuje PostgreSQL; "
            "ht_results.json se už nepoužívá"
        )
    return await _db_set_ht_fight_announcement(
        session_factory, result_id, status, message_id=message_id
    )


# ---------------------------------------------------------------------------
# Čtení HT Fight výsledků (restart-safe)
# ---------------------------------------------------------------------------
async def get_ht_fight_result_for_ticket(
    ticket_id, session_factory: async_sessionmaker[AsyncSession]
) -> dict | None:
    """HT Fight výsledek pro daný ticket (podle ID kanálu), nebo None."""
    if session_factory is None:
        raise RuntimeError(
            "get_ht_fight_result_for_ticket potřebuje PostgreSQL; "
            "ht_results.json se už nepoužívá"
        )
    return await _db_get_ht_fight_result_for_ticket(session_factory, ticket_id)


async def get_ht_fight_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> list:
    """Všechny HT Fight výsledky v pořadí zápisu."""
    if session_factory is None:
        raise RuntimeError(
            "get_ht_fight_results potřebuje PostgreSQL; ht_results.json se už nepoužívá"
        )
    return await _db_get_ht_fight_results(session_factory)


# ---------------------------------------------------------------------------
# PostgreSQL implementace — result_key = f"ht_fight:{result_id}" tak, aby
# commit_promotion_with_wedge (cog) našel řádek po Discord mutaci; HT Fight
# tier se ukládá do Result.subtype, announcement do announcement_status.
# ---------------------------------------------------------------------------
async def _db_record_ht_fight(
    session_factory: async_sessionmaker[AsyncSession],
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
    if now is None:
        now = _now_ms()
    player_id = str(player_id)
    evaluator_id = str(evaluator_id)
    score_clean = (score or "").strip()
    status_clean = (tier_status or "").strip()
    outcome_clean = "Won" if (outcome or "").strip().upper() == "WON" else "Lost"

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

    bridge_clean = normalize_tier(bridge) if (bridge or "").strip() else ""
    if bridge_clean:
        if outcome_clean == "Lost":
            return {"result": "invalid_bridge", "message": _MSG_BRIDGE_ONLY_ON_WIN}
        ok, msg = validate_ht_fight_bridge(bridge_clean)
        if not ok:
            return {"result": "invalid_bridge", "message": msg}

    async with db_transaction(session_factory) as session:
        results = ResultRepository()

        if ticket_id is not None:
            tid = str(ticket_id)
            result_id = f"{tid}{HT_FIGHT_TICKET_KEY_SUFFIX}"
            existing = await results.get_by_key(
                session, f"ht_fight:{result_id}"
            )
            if existing is not None:
                if existing.promotion_status == PROMOTION_COMMITTED:
                    return {
                        "result": "duplicate",
                        "existing": await _db_result_to_dict(session, existing),
                    }
                # H2 audit fix: same reasoning as services.results —
                # discord_pending (a "Won" outcome whose Discord grant never
                # succeeded) must not permanently block retry; discard and
                # let this attempt reprocess cleanly.
                await session.delete(existing)
                await session.flush()
        else:
            kit_row = await KitRepository().get_by_name(session, kit)
            try:
                _outcome, player = await PlayerRepository().claim_discord_id(
                    session, discord_id=int(player_id), ign=(ign or "").strip()
                )
            except PlayerIdentityError as exc:
                return {"result": "identity_conflict", "message": str(exc)}
            dup = await _db_find_recent_ht_fight_duplicate(
                session,
                player.id,
                kit_id=kit_row.id if kit_row is not None else None,
                fight_tier=fight_tier,
                score=score_clean,
                outcome=outcome_clean,
                opponent_id=opponent_id,
                tier_status=status_clean,
                now=now,
            )
            if dup is not None:
                return {
                    "result": "duplicate",
                    "existing": await _db_result_to_dict(session, dup),
                }
            result_id = f"{HT_FIGHT_RESULT_PREFIX}{player_id}-{now}"

        kit_row = await _db_resolve_kit(session, kit)
        if ticket_id is not None:
            tid = str(ticket_id)
            ticket = await TicketRepository().get_by_channel(session, int(tid))
            if ticket is None:
                return {"result": "not_found"}
            if ticket.ticket_type != TICKET_TYPE_FIGHT:
                return {
                    "result": "not_fight_ticket",
                    "ticket": await _db_ticket_to_dict(session, ticket),
                }
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
            try:
                _outcome, player = await PlayerRepository().claim_discord_id(
                    session, discord_id=int(player_id), ign=(ign or "").strip()
                )
            except PlayerIdentityError as exc:
                return {"result": "identity_conflict", "message": str(exc)}

        if kit_row is None:
            return {
                "result": "invalid_argument",
                "message": f"Neznámý kit: {kit}",
            }

        previous = await _db_current_tier_code(session, player.id, kit_row.id)

        current = previous if previous != "N/A" else None
        promoted = None
        if outcome_clean == "Won" and bridge_clean:
            if (
                current
                and current in HT3_TIER_LADDER
                and (
                    HT3_TIER_LADDER.index(bridge_clean)
                    <= HT3_TIER_LADDER.index(current)
                )
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

        prev_tier_row = (
            await _db_resolve_tier(session, previous) if previous != "N/A" else None
        )
        new_tier_row = (
            await _db_resolve_tier(session, promoted) if promoted else None
        )
        bridge_tier_row = (
            await _db_resolve_tier(session, bridge_clean) if bridge_clean else None
        )
        evaluator = (
            await PlayerRepository().get_by_discord_id(session, int(evaluator_id))
            if evaluator_id and evaluator_id.isdigit()
            else None
        )

        row = await results.insert(
            session,
            result_key=f"ht_fight:{result_id}",
            kind="ht_fight",
            subtype=normalize_tier(fight_tier),
            player_id=player.id,
            evaluator_id=evaluator.id if evaluator is not None else None,
            kit_id=kit_row.id,
            ticket_channel_id=int(tid) if ticket_id is not None else None,
            previous_tier_id=prev_tier_row.id if prev_tier_row is not None else None,
            new_tier_id=new_tier_row.id if new_tier_row is not None else None,
            bridge_tier_id=bridge_tier_row.id if bridge_tier_row is not None else None,
            tier_status=status_clean,
            score=score_clean,
            outcome=outcome_clean,
            opponent_id=int(opponent_id) if str(opponent_id or "").isdigit() else None,
            opponent_name=opponent_name,
            notes=(notes or "").strip() or None,
            eval_flag=False,
            date=date or "",
            recorded_at=_ms_to_dt(now),
            promotion_status=(
                PROMOTION_DISCORD_PENDING if outcome_clean == "Won" else None
            ),
        )

        if ticket_id is not None and outcome_clean == "Lost":
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
                action="ht_fight",
                actor_id=int(evaluator_id) if evaluator_id.isdigit() else None,
                actor_name=evaluator_name or "",
                entity_type="ticket",
                entity_id=str(tid),
                details={
                    "details": f"{previous} (prohra)",
                    "ts": int(now),
                },
            )

        record = await _db_ht_fight_to_dict(session, row)
        return {"result": "created", "record": record, "previous_tier": previous}


async def _db_current_tier_code(
    session: AsyncSession, player_id: int, kit_id: int
) -> str:
    mirror = await MirrorRepository().get_current(
        session, player_id=player_id, kit_id=kit_id
    )
    if mirror is None:
        return "N/A"
    tier = await TierDefinitionRepository().get_by_id(session, mirror.tier_id)
    return tier.code if tier is not None else "N/A"


async def _db_find_recent_ht_fight_duplicate(
    session: AsyncSession,
    player_id: int,
    *,
    kit_id,
    fight_tier: str,
    score: str,
    outcome: str,
    opponent_id,
    tier_status: str,
    now: int,
):
    """Volný HT Fight záznam identický s tímto odesláním (dedup), nebo None.

    Db ekvivalent ``_find_recent_ht_fight_duplicate``: hledá jen volné HT
    Fight záznamy (bez ticketu) hráče v okně ``HT_FIGHT_DEDUP_WINDOW_MS``
    a porovnává fingerprint zápasu.
    """
    if kit_id is None:
        return None
    ft = (fight_tier or "").strip().upper()
    sc = (score or "").strip().lower()
    oc = (outcome or "").strip()
    op = str(opponent_id or "").strip()
    ts = (tier_status or "").strip().upper()
    since = _ms_to_dt(now - HT_FIGHT_DEDUP_WINDOW_MS)
    recent = await ResultRepository().list_free_ht_fights(
        session, player_id=player_id, since=since
    )
    for row in recent:
        if str(row.subtype or "").strip().upper() != ft:
            continue
        if str(row.score or "").strip().lower() != sc:
            continue
        if str(row.outcome or "").strip() != oc:
            continue
        if str(row.opponent_id or "").strip() != op:
            continue
        if str(row.tier_status or "").strip().upper() != ts:
            continue
        return row
    return None


async def _db_ht_fight_to_dict(session: AsyncSession, row) -> dict:
    """Rekonstrukce JSON tvaru HT Fight záznamu z DB řádku (kompatibilní dict)."""
    record = await _db_result_to_dict(session, row)
    if row.bridge_tier_id is not None:
        bridge = await TierDefinitionRepository().get_by_id(session, row.bridge_tier_id)
        if bridge is not None:
            record["bridgeTier"] = bridge.code
    return record


async def _db_set_ht_fight_announcement(
    session_factory: async_sessionmaker[AsyncSession],
    result_id: str,
    status: str,
    message_id=None,
) -> dict:
    async with db_transaction(session_factory) as session:
        row = await ResultRepository().set_announcement(
            session,
            result_key=f"ht_fight:{result_id}",
            announcement_status=status,
            announcement_message_id=int(message_id) if message_id else None,
        )
        if row is None:
            return {"result": "not_found"}
        return {"result": "ok", "record": await _db_result_to_dict(session, row)}


async def _db_get_ht_fight_result_for_ticket(
    session_factory: async_sessionmaker[AsyncSession], ticket_id
) -> dict | None:
    async with db_transaction(session_factory) as session:
        tid = str(ticket_id)
        row = await ResultRepository().get_by_key(
            session, f"ht_fight:{tid}{HT_FIGHT_TICKET_KEY_SUFFIX}"
        )
        return await _db_result_to_dict(session, row) if row is not None else None


async def _db_get_ht_fight_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> list:
    async with db_transaction(session_factory) as session:
        rows = await ResultRepository().list_all(session)
        out = []
        for r in rows:
            if r.kind != "ht_fight":
                continue
            out.append(await _db_ht_fight_to_dict(session, r))
        return out
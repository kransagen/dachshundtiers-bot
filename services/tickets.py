"""HT ticket systém – čistá logika + transakční stav (bez discord.py).

Stav žije v ``data/ht_tickets.json`` klíčovaný podle **ID textového kanálu**
ticketu (každý ticket = jeden kanál). Používá se pro:

- automatické vytvoření ticketu (z HT3 panelu),
- prevenci duplicit (hráč nemůže mít víc otevřených ticketů na stejný kit),
- vlastnictví ticketu (ownerId = kdo ho otevřel),
- Claim / Unclaim (kdo si ticket převzal = tester, který test provede),
- /add a /remove členů (+ přístup do kanálu),
- Close / Reopen (close nastaví i HT3+ cooldown),
- log událostí (``data/ht_ticket_logs.json``, restart-safe).

Tvar ticketu::

    {
      "id": "123456789012345678",      # ID kanálu ticketu
      "status": "open" | "closed",
      "ownerId": "111",
      "ownerName": "Hráč",
      "ign": "hrac_ign",
      "kit": "AnchorPvP",              # displejový název kitu (klíč cooldownu)
      "targetTier": "HT3",             # o jaký tier hráč usiluje
      "currentTier": "LT3" | None,     # aktuální tier z players.json v čase otevření
      "eval": true,                    # měl hráč „LT3 + eval"?
      "claimerId": None | "222",       # kdo si ticket převzal
      "claimerName": None | "Tester",
      "members": ["333"],              # hráči přidaní přes /add
      "categoryId": 123,
      "panelMessageId": "999" | None,  # zpráva s embedem a tlačítky
      "createdAt": 1769550000000,
      "closedAt": None | 1769550000000,
    }
"""

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Kit, TierDefinition
from db.repositories.cooldowns import COOLDOWN_HT3, CooldownRepository
from db.repositories.kits import KitRepository, TierDefinitionRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.sync_audit import AuditRepository
from db.repositories.tickets import Ticket, TicketMemberRepository, TicketRepository
from db.repositories.tiers import MirrorRepository
from db.services.session import transaction
from services import store
from storage import load_data

HT_TICKETS_FILE = "ht_tickets.json"
HT_TICKET_LOGS_FILE = "ht_ticket_logs.json"
HT3_COOLDOWNS_FILE = "ht3_cooldowns.json"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

# Typ ticketu: HT3+ eval tickety (vytváří HT3 panel) versus HT Fight tickety
# (pro /topresult). Běžný HT3 panel zakládá tickety typu ``eval``; HT Fight
# tickety zatím nevytváří žádný cog – pole slouží jako jednoznačný identifikátor
# pro validaci /topresult („verify the ticket is an HT Fight ticket").
TICKET_TYPE_EVAL = "eval"
TICKET_TYPE_FIGHT = "fight"


def get_ticket_type(ticket: dict) -> str:
    """Typ ticketu (``eval`` | ``fight``); staré záznamy bez pole = ``eval``."""
    if not isinstance(ticket, dict):
        return TICKET_TYPE_EVAL
    return ticket.get("ticketType") or TICKET_TYPE_EVAL


def is_ht_fight_ticket(ticket: dict) -> bool:
    """Je ticket HT Fight ticket? (pouze takové akceptuje /topresult)"""
    return get_ticket_type(ticket) == TICKET_TYPE_FIGHT

# ---------------------------------------------------------------------------
# HT3+ ticket žebříček a pomocné funkce tierů (přesunuto z views.py – testovatelné)
# ---------------------------------------------------------------------------
# (nejhorší → nejlepší, potvrzeno provozovatelem):
#   LT5 < HT5 < LT4 < HT4 < LT3 < LT3+eval < HT3 < LT2 < HT2 < LT1 < HT1
# „LT3+eval" (v kódu LT3E) je status mezi LT3 a HT3: hráč má pořád roli LT3,
# ale s evalem může otevírat HT3+ tickety. Evaly se drží v data/evals.json.
HT3_TIER_LADDER = [
    "LT5", "HT5", "LT4", "HT4", "LT3", "LT3E", "HT3", "LT2", "HT2", "LT1", "HT1",
]


def next_ticket_tier(current: str) -> str | None:
    """O stupeň lepší tier – limit hráče (repríza žebříčku bez virtuálního LT3E).

    Příklad: LT3 → HT3, HT3 → LT2, LT2 → HT2, HT1 → HT1 (vrchol).
    „LT3E" je jen virtuální status (LT3 + eval), ne reálný tier – při výpočtu
    limitu se přeskočí (LT3 i LT3E míří na HT3).
    Vrací None, pokud tier nelze přečíst (R-tiery / neznámý formát).
    """
    t = (current or "").strip().upper()
    if t == "LT3E":
        t = "LT3"  # virtuální status – limit se počítá jako u LT3
    real_ladder = [x for x in HT3_TIER_LADDER if x != "LT3E"]
    if t not in real_ladder:
        return None  # R-tiery / neznámý formát – nekontrolujeme
    idx = real_ladder.index(t)
    if idx == len(real_ladder) - 1:
        return t  # HT1 = vrchol žebříčku
    return real_ladder[idx + 1]


def tier_allows_tickets(tier: str) -> bool:
    """Může hráč otevírat HT3+ tickety podle svého tieru? (LT3+eval a výš)"""
    t = (tier or "").strip().upper()
    if t not in HT3_TIER_LADDER:
        return False
    return HT3_TIER_LADDER.index(t) >= HT3_TIER_LADDER.index("LT3E")


def effective_ticket_tier(current_tier: str | None, eval_ok: bool) -> str | None:
    """Tier, ze kterého se počítá limit ticketu.

    Hráč se záznamem „LT3+eval" (eval_ok) se chová jako když má tier LT3E –
    i když má zapsaný jen LT3 nebo žádný – aby mohl ticket zacílit na HT3.
    """
    if not eval_ok:
        return current_tier
    cur = (current_tier or "").strip().upper()
    eval_idx = HT3_TIER_LADDER.index("LT3E")
    if cur not in HT3_TIER_LADDER:
        return "LT3E"
    return cur if HT3_TIER_LADDER.index(cur) >= eval_idx else "LT3E"


def find_player_tier(ign: str, kit: str, discord_id=None) -> str | None:
    """Aktuální tier hráče pro daný kit; Discord ID má přednost před IGN.

    ``discord_id`` je primární identita (services/player_identity.py) – když
    hráče podle Discord ID najdeme, IGN se ignoruje. Bez Discord ID klasická
    case-insensitive shoda podle IGN (legacy chování).
    """
    try:
        players = load_data("players.json", []) or []
    except Exception:
        return None
    player = None
    if discord_id:
        did = str(discord_id)
        player = next(
            (
                p
                for p in players
                if isinstance(p, dict) and str(p.get("discordId") or "") == did
            ),
            None,
        )
    if player is None:
        player = next(
            (
                p
                for p in players
                if isinstance(p, dict)
                and str(p.get("username", "")).strip().lower()
                == (ign or "").strip().lower()
            ),
            None,
        )
    if player is None:
        return None
    modes = player.get("modes") or {}
    tier = modes.get(kit)
    return str(tier).strip().upper() if tier else None


async def player_tier(
    ign: str,
    kit: str,
    discord_id=None,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> str | None:
    """Aktuální tier hráče pro daný kit (dual-mode).

    DB režim: zdroj pravdy je mirror tabulky aktuálního tieru (Discord-confirmed;
    viz F10 – stávající tier se v DB neodvozuje z JSON). JSON režim deleguje na
    sync ``find_player_tier`` (players.json, legacy).
    """
    if session_factory is not None:
        async with transaction(session_factory) as session:
            discord_int = int(discord_id) if discord_id else None
            player, _source = await PlayerRepository().resolve(
                session,
                discord_id=discord_int,
                ign=(ign or "").strip() or None,
            )
            if player is None:
                return None
            kit_row = await KitRepository().get_by_name(session, (kit or "").strip())
            if kit_row is None:
                return None
            mirror = await MirrorRepository().get_current(
                session, player_id=player.id, kit_id=kit_row.id
            )
            if mirror is None:
                return None
            tier = await TierDefinitionRepository().get_by_id(
                session, mirror.tier_id
            )
            return tier.code if tier is not None else None

    return find_player_tier(ign, kit, discord_id=discord_id)


# ---------------------------------------------------------------------------
# Čtení stavu
# ---------------------------------------------------------------------------
async def get_tickets(session_factory: async_sessionmaker[AsyncSession] | None = None) -> dict:
    if session_factory is not None:
        return await _db_get_tickets(session_factory)
    return await store.read(HT_TICKETS_FILE, {})


async def get_ticket(
    channel_id,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Vrátí ticket podle ID kanálu (nebo None)."""
    if session_factory is not None:
        return await _db_get_ticket(session_factory, channel_id)
    tickets = await store.read(HT_TICKETS_FILE, {})
    return tickets.get(str(channel_id))


async def find_open_ticket(
    owner_id,
    kit: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict | None:
    """Otevřený ticket hráče na daný kit (prevence duplicit)."""
    if session_factory is not None:
        return await _db_find_open_ticket(session_factory, owner_id, kit)
    owner_id = str(owner_id)
    kit_key = str(kit).strip().lower()
    tickets = await store.read(HT_TICKETS_FILE, {})
    for ticket in tickets.values():
        if not isinstance(ticket, dict):
            continue
        if (
            ticket.get("status") == STATUS_OPEN
            and str(ticket.get("ownerId", "")) == owner_id
            and str(ticket.get("kit", "")).strip().lower() == kit_key
        ):
            return ticket
    return None


def make_ticket(
    *,
    channel_id,
    owner_id: str,
    owner_name: str,
    ign: str,
    kit: str,
    target_tier: str,
    current_tier: str | None,
    eval_ok: bool,
    category_id: int,
    panel_message_id=None,
    ticket_type: str = TICKET_TYPE_EVAL,
    now: int,
) -> dict:
    """Sestaví nový ticket (bez zápisu)."""
    return {
        "id": str(channel_id),
        "status": STATUS_OPEN,
        "ownerId": str(owner_id),
        "ownerName": owner_name or "",
        "ign": ign or "",
        "kit": kit or "",
        "targetTier": (target_tier or "").strip().upper(),
        "currentTier": current_tier,
        "eval": bool(eval_ok),
        "claimerId": None,
        "claimerName": None,
        "members": [],
        "categoryId": category_id,
        "panelMessageId": str(panel_message_id) if panel_message_id else None,
        "ticketType": ticket_type or TICKET_TYPE_EVAL,
        "createdAt": now,
        "closedAt": None,
    }


# ---------------------------------------------------------------------------
# Transakční operace
# ---------------------------------------------------------------------------
async def create_ticket(
    *,
    channel_id,
    owner_id: str,
    owner_name: str,
    ign: str,
    kit: str,
    target_tier: str,
    current_tier: str | None,
    eval_ok: bool,
    category_id: int,
    panel_message_id=None,
    ticket_type: str = TICKET_TYPE_EVAL,
    now: int,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Transakčně vytvoří ticket (prevence duplicit uvnitř kritického úseku).

    Vrací ``{"result": "created", "ticket": {...}}``, nebo
    ``{"result": "duplicate", "ticket": {existující otevřený ticket}}``.
    """
    owner_id = str(owner_id)
    kit_key = str(kit).strip().lower()
    channel_id = str(channel_id)

    if session_factory is not None:
        return await _db_create_ticket(
            session_factory,
            channel_id=channel_id,
            owner_id=owner_id,
            owner_name=owner_name,
            ign=ign,
            kit=kit,
            target_tier=target_tier,
            current_tier=current_tier,
            eval_ok=eval_ok,
            category_id=category_id,
            panel_message_id=panel_message_id,
            ticket_type=ticket_type,
            now=now,
        )

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        existing = next(
            (
                t
                for t in tickets.values()
                if isinstance(t, dict)
                and t.get("status") == STATUS_OPEN
                and str(t.get("ownerId", "")) == owner_id
                and str(t.get("kit", "")).strip().lower() == kit_key
            ),
            None,
        )
        if existing is not None:
            return {"result": "duplicate", "ticket": existing}

        ticket = make_ticket(
            channel_id=channel_id,
            owner_id=owner_id,
            owner_name=owner_name,
            ign=ign,
            kit=kit,
            target_tier=target_tier,
            current_tier=current_tier,
            eval_ok=eval_ok,
            category_id=category_id,
            panel_message_id=panel_message_id,
            ticket_type=ticket_type,
            now=now,
        )
        tickets[channel_id] = ticket
        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "created", "ticket": ticket}

    return await store.transaction((HT_TICKETS_FILE,), _run)


async def claim_ticket(
    channel_id,
    actor_id: str,
    actor_name: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Ticket si převezme tester (actor); bez přepisu cizího claimu."""
    channel_id = str(channel_id)
    actor_id = str(actor_id)

    if session_factory is not None:
        return await _db_claim_ticket(session_factory, channel_id, actor_id, actor_name)

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        ticket = tickets.get(channel_id)
        if ticket is None:
            return {"result": "not_found"}
        if ticket.get("status") != STATUS_OPEN:
            return {"result": "not_open"}
        if actor_id == str(ticket.get("ownerId", "")):
            return {"result": "own_ticket"}
        if ticket.get("claimerId") and str(ticket["claimerId"]) != actor_id:
            return {
                "result": "already_claimed",
                "claimer_id": str(ticket["claimerId"]),
                "claimer_name": ticket.get("claimerName") or "",
            }
        ticket["claimerId"] = actor_id
        ticket["claimerName"] = actor_name or ""
        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "claimed", "ticket": ticket}

    return await store.transaction((HT_TICKETS_FILE,), _run)


async def unclaim_ticket(
    channel_id,
    actor_id: str,
    *,
    force: bool = False,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Zruší claim ticketu; bez ``force`` jen aktér sám (kontrola claimera)."""
    channel_id = str(channel_id)
    actor_id = str(actor_id)

    if session_factory is not None:
        return await _db_unclaim_ticket(
            session_factory, channel_id, actor_id, force=force
        )

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        ticket = tickets.get(channel_id)
        if ticket is None:
            return {"result": "not_found"}
        if not ticket.get("claimerId"):
            return {"result": "not_claimed"}
        if not force and str(ticket["claimerId"]) != actor_id:
            return {
                "result": "not_claimer",
                "claimer_id": str(ticket["claimerId"]),
                "claimer_name": ticket.get("claimerName") or "",
            }
        previous = {
            "claimer_id": str(ticket["claimerId"]),
            "claimer_name": ticket.get("claimerName") or "",
        }
        ticket["claimerId"] = None
        ticket["claimerName"] = None
        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "unclaimed", "ticket": ticket, "previous": previous}

    return await store.transaction((HT_TICKETS_FILE,), _run)


async def add_member(
    channel_id,
    member_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    member_name: str | None = None,
) -> dict:
    """Přidá člena týmu ticketu; jen vlastník-id a otevřený ticket."""
    channel_id = str(channel_id)
    member_id = str(member_id)

    if session_factory is not None:
        return await _db_add_member(
            session_factory, channel_id, member_id, member_name
        )

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        ticket = tickets.get(channel_id)
        if ticket is None:
            return {"result": "not_found"}
        if ticket.get("status") != STATUS_OPEN:
            return {"result": "not_open"}
        if member_id == str(ticket.get("ownerId", "")):
            return {"result": "is_owner"}
        members = ticket.setdefault("members", [])
        if member_id in members:
            return {"result": "already_member", "ticket": ticket}
        members.append(member_id)
        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "added", "ticket": ticket}

    return await store.transaction((HT_TICKETS_FILE,), _run)


async def remove_member(
    channel_id,
    member_id: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Odebere člena týmu ticketu; jen vlastník-id a otevřený ticket."""
    channel_id = str(channel_id)
    member_id = str(member_id)

    if session_factory is not None:
        return await _db_remove_member(session_factory, channel_id, member_id)

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        ticket = tickets.get(channel_id)
        if ticket is None:
            return {"result": "not_found"}
        if member_id == str(ticket.get("ownerId", "")):
            return {"result": "is_owner"}
        if ticket.get("claimerId") and member_id == str(ticket["claimerId"]):
            return {"result": "is_claimer"}
        members = ticket.setdefault("members", [])
        if member_id not in members:
            return {"result": "not_member", "ticket": ticket}
        members.remove(member_id)
        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "removed", "ticket": ticket}

    return await store.transaction((HT_TICKETS_FILE,), _run)


async def close_ticket(
    channel_id,
    actor_id: str,
    *,
    cooldown_ms: int = 0,
    now: int = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Zavře ticket + nastaví HT3+ cooldown vlastníkovi (7 dní).

    Kanál zůstává (kvůli Reopen / logování), jen stav je ``closed`` a hráči se
    v tomhle kitu do dalšího cooldownu neotevře nový ticket. Vrací dict
    s klíčem ``result`` a ``ticket``.
    """
    channel_id = str(channel_id)
    actor_id = str(actor_id)
    if now is None:
        now = _now_ms()

    if session_factory is not None:
        return await _db_close_ticket(
            session_factory, channel_id, actor_id, cooldown_ms=cooldown_ms, now=now
        )

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        ticket = tickets.get(channel_id)
        if ticket is None:
            return {"result": "not_found"}
        if ticket.get("status") == STATUS_CLOSED:
            return {"result": "already_closed", "ticket": ticket}
        ticket["status"] = STATUS_CLOSED
        ticket["closedAt"] = now

        if cooldown_ms > 0:
            cooldowns = tx.get(HT3_COOLDOWNS_FILE, {})
            owner_id = str(ticket.get("ownerId", ""))
            if owner_id:
                # klíč cooldownu = displejový název kitu (viz HT3 panel)
                cooldowns.setdefault(owner_id, {})[str(ticket.get("kit", ""))] = now + cooldown_ms
                tx.set(HT3_COOLDOWNS_FILE, cooldowns)

        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "closed", "ticket": ticket}

    return await store.transaction(
        (HT_TICKETS_FILE, HT3_COOLDOWNS_FILE) if cooldown_ms > 0 else (HT_TICKETS_FILE,),
        _run,
    )


def _cooldown_remaining_ms(
    cooldowns: dict, owner_id: str, kit_key: str, now: int
) -> int | None:
    """Zbývající HT3+ cooldown hráče na kit (ms); None = žádný/vypršel."""
    if not owner_id or not kit_key:
        return None
    expires = (cooldowns.get(owner_id) or {}).get(kit_key)
    if not expires:
        return None
    remaining = int(expires) - now
    return remaining if remaining > 0 else None


async def reopen_ticket(
    channel_id,
    actor_id: str,
    *,
    cooldown_ms: int = 0,
    now: int = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Znovu otevře zavřený ticket (Reopen).

    ``cooldown_ms`` > 0 – pokud má vlastník ticketu na daný kit ještě aktivní
    HT3+ cooldown (data/ht3_cooldowns.json), ticket se NEOTEVŘE a vrátí
    ``{"result": "cooldown", "remaining_ms", "kit", "ticket"}``. Výchozí 0
    zachovává původní chování bez cooldown kontroly.
    """
    channel_id = str(channel_id)
    actor_id = str(actor_id)
    if now is None:
        now = _now_ms()

    if session_factory is not None:
        return await _db_reopen_ticket(
            session_factory, channel_id, actor_id, cooldown_ms=cooldown_ms, now=now
        )

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        ticket = tickets.get(channel_id)
        if ticket is None:
            return {"result": "not_found"}
        if ticket.get("status") != STATUS_CLOSED:
            return {"result": "not_closed", "ticket": ticket}

        if cooldown_ms > 0:
            cooldowns = tx.get(HT3_COOLDOWNS_FILE, {})
            remaining = _cooldown_remaining_ms(
                cooldowns,
                str(ticket.get("ownerId", "")),
                str(ticket.get("kit", "")),
                now,
            )
            if remaining is not None:
                return {
                    "result": "cooldown",
                    "remaining_ms": remaining,
                    "kit": ticket.get("kit", ""),
                    "ticket": ticket,
                }

        ticket["status"] = STATUS_OPEN
        ticket["closedAt"] = None
        tx.set(HT_TICKETS_FILE, tickets)
        return {"result": "reopened", "ticket": ticket}

    files = (
        (HT_TICKETS_FILE, HT3_COOLDOWNS_FILE)
        if cooldown_ms > 0
        else (HT_TICKETS_FILE,)
    )
    return await store.transaction(files, _run)


# ---------------------------------------------------------------------------
# Log událostí (data/ht_ticket_logs.json) – restart-safe
# ---------------------------------------------------------------------------
async def log_ticket_event(
    ticket_id,
    action: str,
    actor_id: str,
    actor_name: str = "",
    details: str = None,
    now: int = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Připíše událost do logu ticketu (append-only)."""
    if now is None:
        now = _now_ms()

    if session_factory is not None:
        return await _db_log_ticket_event(
            session_factory,
            ticket_id,
            action=action,
            actor_id=actor_id,
            actor_name=actor_name,
            details=details,
            now=now,
        )

    async def _run(tx):
        logs = tx.get(HT_TICKET_LOGS_FILE, {})
        bucket = logs.setdefault(str(ticket_id), [])
        bucket.append(
            {
                "ts": now,
                "action": action,
                "actorId": str(actor_id),
                "actorName": actor_name or "",
                "details": details,
            }
        )
        tx.set(HT_TICKET_LOGS_FILE, logs)

    return await store.transaction((HT_TICKET_LOGS_FILE,), _run)


async def get_ticket_logs(
    ticket_id,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> list:
    """Vrátí události daného ticketu (nejstarší první)."""
    if session_factory is not None:
        return await _db_get_ticket_logs(session_factory, ticket_id)
    logs = await store.read(HT_TICKET_LOGS_FILE, {})
    return logs.get(str(ticket_id), [])


async def set_panel_message(
    channel_id,
    message_id,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Doplní ``panelMessageId`` do záznamu ticketu (po vytvoření zprávy)."""
    if session_factory is not None:
        return await _db_set_panel_message(session_factory, channel_id, message_id)

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        rec = tickets.get(str(channel_id))
        if rec is None:
            return None
        rec["panelMessageId"] = str(message_id)
        tx.set(HT_TICKETS_FILE, tickets)
        return rec

    return await store.transaction((HT_TICKETS_FILE,), _run)


# ---------------------------------------------------------------------------
# PostgreSQL režim (session_factory) – JSON-mód zůstává beze změny výše
# ---------------------------------------------------------------------------
def _db_now() -> datetime:
    return datetime.now(timezone.utc)


def _ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _dt_to_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


async def _db_resolve_kit(session: AsyncSession, name: str) -> Kit | None:
    clean = (name or "").strip()
    if not clean:
        return None
    kit = await KitRepository().get_by_name(session, clean)
    if kit is not None:
        return kit
    return await KitRepository().get_or_create(
        session, key=clean.lower(), name=clean
    )


async def _db_resolve_tier(
    session: AsyncSession, code: str | None
) -> TierDefinition | None:
    if not code or not str(code).strip():
        return None
    code = str(code).strip().upper()
    tier = await TierDefinitionRepository().get_by_code(session, code)
    if tier is not None:
        return tier
    return await TierDefinitionRepository().get_or_create(
        session,
        code=code,
        kind="virtual" if code == "LT3E" else "ladder",
        display_name=code,
    )


async def _db_ticket_to_dict(session: AsyncSession, t: Ticket) -> dict:
    owner = await PlayerRepository().get_by_id(session, t.player_id)
    claimer = (
        await PlayerRepository().get_by_id(session, t.claimer_id)
        if t.claimer_id is not None
        else None
    )
    kit = await KitRepository().get_by_id(session, t.kit_id)
    target_tier = (
        await TierDefinitionRepository().get_by_id(session, t.target_tier_id)
        if t.target_tier_id is not None
        else None
    )
    current_tier = (
        await TierDefinitionRepository().get_by_id(session, t.current_tier_id)
        if t.current_tier_id is not None
        else None
    )
    members = [
        m
        for m in await TicketMemberRepository().list_for_ticket(
            session, ticket_id=t.id
        )
        if m.removed_at is None
    ]
    member_ids: list[str] = []
    for m in members:
        mp = await PlayerRepository().get_by_id(session, m.player_id)
        did = mp.discord_id if mp is not None else None
        member_ids.append(str(did if did is not None else m.player_id))
    owner_did = owner.discord_id if owner is not None else None
    claimer_did = claimer.discord_id if claimer is not None else None
    return {
        "id": str(t.channel_id),
        "status": t.status,
        "ownerId": str(owner_did if owner_did is not None else t.player_id),
        "ownerName": t.owner_name or "",
        "ign": t.ign or "",
        "kit": kit.name if kit is not None else "",
        "targetTier": target_tier.code if target_tier is not None else "",
        "currentTier": current_tier.code if current_tier is not None else None,
        "eval": bool(t.eval),
        "claimerId": (
            str(claimer_did) if claimer_did is not None else None
        ),
        "claimerName": t.claimer_name or "",
        "members": member_ids,
        "categoryId": t.category_id,
        "panelMessageId": (
            str(t.panel_message_id) if t.panel_message_id else None
        ),
        "ticketType": t.ticket_type,
        "createdAt": _dt_to_ms(t.created_at) or 0,
        "closedAt": _dt_to_ms(t.closed_at),
    }


async def _db_get_tickets(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict:
    async with transaction(session_factory) as session:
        out: dict = {}
        for t in await TicketRepository().list_all(session):
            out[str(t.channel_id)] = await _db_ticket_to_dict(session, t)
        return out


async def _db_get_ticket(
    session_factory: async_sessionmaker[AsyncSession], channel_id
) -> dict | None:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        return await _db_ticket_to_dict(session, t) if t is not None else None


async def _db_find_open_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    owner_id,
    kit: str,
) -> dict | None:
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(
            session, int(owner_id)
        )
        if player is None:
            return None
        kit_row = await KitRepository().get_by_name(session, kit)
        if kit_row is None:
            return None
        rows = await TicketRepository().list_open(
            session, player_id=player.id, kit_id=kit_row.id
        )
        return await _db_ticket_to_dict(session, rows[0]) if rows else None


async def _db_create_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channel_id,
    owner_id: str,
    owner_name: str,
    ign: str,
    kit: str,
    target_tier: str,
    current_tier: str | None,
    eval_ok: bool,
    category_id: int,
    panel_message_id=None,
    ticket_type: str = TICKET_TYPE_EVAL,
    now: int,
) -> dict:
    async with transaction(session_factory) as session:
        try:
            _, player = await PlayerRepository().claim_discord_id(
                session,
                discord_id=int(owner_id),
                ign=(ign or "").strip() or (owner_name or "").strip() or str(owner_id),
            )
        except PlayerIdentityError as err:
            return {"result": "identity_conflict", "message": str(err)}
        kit_row = await _db_resolve_kit(session, kit)
        if kit_row is None:
            return {"result": "invalid_kit", "message": "Kit je prázdný."}
        target = await _db_resolve_tier(session, target_tier)
        cur = await _db_resolve_tier(session, current_tier)
        existing = await TicketRepository().list_open(
            session, player_id=player.id, kit_id=kit_row.id
        )
        if existing:
            return {
                "result": "duplicate",
                "ticket": await _db_ticket_to_dict(session, existing[0]),
            }
        # M5 audit fix: list_open() above (read) then open() below (write)
        # is a TOCTOU race — two concurrent create_ticket calls for the
        # same player+kit can both pass the pre-check and race on INSERT.
        # The partial unique index (uq_tickets_open_player_kit) correctly
        # rejects the loser at the DB level, but without this SAVEPOINT +
        # catch, that raw IntegrityError propagated uncaught all the way to
        # the Discord command handler (a generic "unexpected error" instead
        # of a clean "you already have an open ticket").
        try:
            async with session.begin_nested():
                t = await TicketRepository().open(
                    session,
                    channel_id=int(channel_id),
                    player_id=player.id,
                    ign=(ign or "").strip(),
                    kit_id=kit_row.id,
                    target_tier_id=target.id if target is not None else None,
                    current_tier_id=cur.id if cur is not None else None,
                    eval=bool(eval_ok),
                    ticket_type=ticket_type or TICKET_TYPE_EVAL,
                    created_at=_ms_to_dt(now) or _db_now(),
                    owner_name=owner_name or "",
                    category_id=int(category_id) if category_id else None,
                    panel_message_id=(
                        int(panel_message_id) if panel_message_id else None
                    ),
                )
        except IntegrityError:
            winner = await TicketRepository().list_open(
                session, player_id=player.id, kit_id=kit_row.id
            )
            if winner:
                return {
                    "result": "duplicate",
                    "ticket": await _db_ticket_to_dict(session, winner[0]),
                }
            raise
        return {"result": "created", "ticket": await _db_ticket_to_dict(session, t)}


async def _db_claim_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    actor_id: str,
    actor_name: str,
) -> dict:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        if t is None:
            return {"result": "not_found"}
        if t.status != STATUS_OPEN:
            return {"result": "not_open"}
        owner = await PlayerRepository().get_by_id(session, t.player_id)
        if (
            owner is not None
            and owner.discord_id is not None
            and str(owner.discord_id) == actor_id
        ):
            return {"result": "own_ticket"}
        if t.claimer_id is not None:
            claimer = await PlayerRepository().get_by_id(session, t.claimer_id)
            if claimer is None or str(claimer.discord_id) != actor_id:
                return {
                    "result": "already_claimed",
                    "claimer_id": (
                        str(claimer.discord_id)
                        if claimer is not None and claimer.discord_id is not None
                        else str(t.claimer_id)
                    ),
                    "claimer_name": t.claimer_name or "",
                }
        try:
            actor = await PlayerRepository().get_or_create_by_discord_id(
                session,
                discord_id=int(actor_id),
                ign=(actor_name or "").strip() or str(actor_id),
            )
        except PlayerIdentityError as err:
            return {"result": "identity_conflict", "message": str(err)}
        row = await TicketRepository().claim(
            session,
            ticket_id=t.id,
            claimer_id=actor.id,
            claimer_name=actor_name or "",
        )
        return {"result": "claimed", "ticket": await _db_ticket_to_dict(session, row)}


async def _db_unclaim_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    actor_id: str,
    *,
    force: bool = False,
) -> dict:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        if t is None:
            return {"result": "not_found"}
        if t.claimer_id is None:
            return {"result": "not_claimed"}
        claimer = await PlayerRepository().get_by_id(session, t.claimer_id)
        if not force and (
            claimer is None or str(claimer.discord_id) != actor_id
        ):
            return {
                "result": "not_claimer",
                "claimer_id": (
                    str(claimer.discord_id)
                    if claimer is not None and claimer.discord_id is not None
                    else str(t.claimer_id)
                ),
                "claimer_name": t.claimer_name or "",
            }
        previous = {
            "claimer_id": (
                str(claimer.discord_id)
                if claimer is not None and claimer.discord_id is not None
                else str(t.claimer_id)
            ),
            "claimer_name": t.claimer_name or "",
        }
        row = await TicketRepository().claim(
            session, ticket_id=t.id, claimer_id=None, claimer_name=None
        )
        return {
            "result": "unclaimed",
            "ticket": await _db_ticket_to_dict(session, row),
            "previous": previous,
        }


async def _db_add_member(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    member_id: str,
    member_name: str | None,
) -> dict:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        if t is None:
            return {"result": "not_found"}
        if t.status != STATUS_OPEN:
            return {"result": "not_open"}
        owner = await PlayerRepository().get_by_id(session, t.player_id)
        if (
            owner is not None
            and owner.discord_id is not None
            and str(owner.discord_id) == member_id
        ):
            return {"result": "is_owner"}
        member = await PlayerRepository().get_by_discord_id(
            session, int(member_id)
        )
        if member is None:
            if not (member_name or "").strip():
                return {
                    "result": "player_not_found",
                    "ticket": await _db_ticket_to_dict(session, t),
                }
            try:
                member = await PlayerRepository().get_or_create_by_discord_id(
                    session,
                    discord_id=int(member_id),
                    ign=(member_name or "").strip(),
                )
            except PlayerIdentityError as err:
                return {"result": "identity_conflict", "message": str(err)}
        if await TicketMemberRepository().has_member(
            session, ticket_id=t.id, player_id=member.id
        ):
            return {
                "result": "already_member",
                "ticket": await _db_ticket_to_dict(session, t),
            }
        await TicketMemberRepository().add(
            session, ticket_id=t.id, player_id=member.id
        )
        return {"result": "added", "ticket": await _db_ticket_to_dict(session, t)}


async def _db_remove_member(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    member_id: str,
) -> dict:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        if t is None:
            return {"result": "not_found"}
        owner = await PlayerRepository().get_by_id(session, t.player_id)
        if (
            owner is not None
            and owner.discord_id is not None
            and str(owner.discord_id) == member_id
        ):
            return {"result": "is_owner"}
        member = await PlayerRepository().get_by_discord_id(
            session, int(member_id)
        )
        member_row = None
        if member is not None:
            member_row = await TicketMemberRepository().get(
                session, ticket_id=t.id, player_id=member.id
            )
        if t.claimer_id is not None:
            claimer = await PlayerRepository().get_by_id(session, t.claimer_id)
            if (
                claimer is not None
                and claimer.discord_id is not None
                and str(claimer.discord_id) == member_id
            ):
                return {"result": "is_claimer"}
        if member_row is None:
            return {
                "result": "not_member",
                "ticket": await _db_ticket_to_dict(session, t),
            }
        await TicketMemberRepository().remove(
            session, ticket_id=t.id, player_id=member.id
        )
        return {"result": "removed", "ticket": await _db_ticket_to_dict(session, t)}


async def _db_close_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    actor_id: str,
    *,
    cooldown_ms: int = 0,
    now: int,
) -> dict:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        if t is None:
            return {"result": "not_found"}
        if t.status == STATUS_CLOSED:
            return {
                "result": "already_closed",
                "ticket": await _db_ticket_to_dict(session, t),
            }
        row = await TicketRepository().close_by_channel(
            session, channel_id=int(channel_id), closed_at=_ms_to_dt(now)
        )
        if cooldown_ms > 0 and row is not None:
            await CooldownRepository().upsert(
                session,
                player_id=row.player_id,
                cooldown_type=COOLDOWN_HT3,
                kit_id=row.kit_id,
                expires_at=_ms_to_dt(now + int(cooldown_ms)) or _db_now(),
                source="ticket_close",
            )
        return {
            "result": "closed",
            "ticket": await _db_ticket_to_dict(session, row),
        }


async def _db_reopen_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    actor_id: str,
    *,
    cooldown_ms: int = 0,
    now: int,
) -> dict:
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, int(channel_id))
        if t is None:
            return {"result": "not_found"}
        if t.status != STATUS_CLOSED:
            return {
                "result": "not_closed",
                "ticket": await _db_ticket_to_dict(session, t),
            }
        if cooldown_ms > 0:
            now_dt = _ms_to_dt(now)
            active = await CooldownRepository().get_active(
                session,
                player_id=t.player_id,
                cooldown_type=COOLDOWN_HT3,
                kit_id=t.kit_id,
                now=now_dt,
            )
            if active:
                remaining_ms = int(
                    (active[0].expires_at - now_dt).total_seconds() * 1000
                )
                kit = await KitRepository().get_by_id(session, t.kit_id)
                return {
                    "result": "cooldown",
                    "remaining_ms": max(remaining_ms, 0),
                    "kit": kit.name if kit is not None else "",
                    "ticket": await _db_ticket_to_dict(session, t),
                }
        row = await TicketRepository().reopen_by_channel(
            session, channel_id=int(channel_id)
        )
        return {
            "result": "reopened",
            "ticket": await _db_ticket_to_dict(session, row),
        }


async def _db_log_ticket_event(
    session_factory: async_sessionmaker[AsyncSession],
    ticket_id,
    *,
    action: str,
    actor_id: str,
    actor_name: str,
    details: str | None,
    now: int,
) -> None:
    async with transaction(session_factory) as session:
        await AuditRepository().append(
            session,
            action=action or "",
            actor_id=int(actor_id) if actor_id else None,
            actor_name=actor_name or "",
            entity_type="ticket",
            entity_id=str(ticket_id),
            details={"details": details, "ts": int(now)},
        )


async def _db_get_ticket_logs(
    session_factory: async_sessionmaker[AsyncSession], ticket_id
) -> list:
    async with transaction(session_factory) as session:
        rows = await AuditRepository().list(
            session,
            entity_type="ticket",
            entity_id=str(ticket_id),
            limit=500,
        )
        rows_sorted = sorted(rows, key=lambda r: r.created_at)
        out = []
        for r in rows_sorted:
            raw_details = r.details if isinstance(r.details, dict) else None
            out.append(
                {
                    "ts": _dt_to_ms(r.created_at) or 0,
                    "action": r.action or "",
                    "actorId": (
                        str(r.actor_id) if r.actor_id is not None else ""
                    ),
                    "actorName": r.actor_name or "",
                    "details": (
                        raw_details.get("details") if raw_details else None
                    ),
                }
            )
        return out


async def _db_set_panel_message(
    session_factory: async_sessionmaker[AsyncSession],
    channel_id,
    message_id,
) -> dict | None:
    async with transaction(session_factory) as session:
        row = await TicketRepository().set_panel_message(
            session,
            channel_id=int(channel_id),
            message_id=int(message_id),
        )
        return await _db_ticket_to_dict(session, row) if row else None


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
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


# ---------------------------------------------------------------------------
# Čtení stavu
# ---------------------------------------------------------------------------
async def get_tickets() -> dict:
    return await store.read(HT_TICKETS_FILE, {})


async def get_ticket(channel_id) -> dict | None:
    """Vrátí ticket podle ID kanálu (nebo None)."""
    tickets = await store.read(HT_TICKETS_FILE, {})
    return tickets.get(str(channel_id))


async def find_open_ticket(owner_id, kit: str) -> dict | None:
    """Otevřený ticket hráče na daný kit (prevence duplicit)."""
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
) -> dict:
    """Transakčně vytvoří ticket (prevence duplicit uvnitř kritického úseku).

    Vrací ``{"result": "created", "ticket": {...}}``, nebo
    ``{"result": "duplicate", "ticket": {existující otevřený ticket}}``.
    """
    owner_id = str(owner_id)
    kit_key = str(kit).strip().lower()
    channel_id = str(channel_id)

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


async def claim_ticket(channel_id, actor_id: str, actor_name: str) -> dict:
    """Tester si převezme ticket (Claim HT). Vrací dict s klíčem ``result``."""
    channel_id = str(channel_id)
    actor_id = str(actor_id)

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


async def unclaim_ticket(channel_id, actor_id: str, *, force: bool = False) -> dict:
    """Vzdát se ticketu (jen aktuální claimer; ``force`` = kdokoli tester)."""
    channel_id = str(channel_id)
    actor_id = str(actor_id)

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


async def add_member(channel_id, member_id: str) -> dict:
    """Přidá hráče mezi členy ticketu (přístup mu udělí Discord vrstva)."""
    channel_id = str(channel_id)
    member_id = str(member_id)

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


async def remove_member(channel_id, member_id: str) -> dict:
    """Odebere hráče z ticketu (přístup mu Discord vrstva zruší)."""
    channel_id = str(channel_id)
    member_id = str(member_id)

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
    channel_id, actor_id: str, *, cooldown_ms: int = 0, now: int = None
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
) -> None:
    """Připíše událost do logu ticketu (append-only)."""
    if now is None:
        now = _now_ms()

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


async def get_ticket_logs(ticket_id) -> list:
    """Vrátí události daného ticketu (nejstarší první)."""
    logs = await store.read(HT_TICKET_LOGS_FILE, {})
    return logs.get(str(ticket_id), [])


async def set_panel_message(channel_id, message_id) -> None:
    """Doplní ``panelMessageId`` do záznamu ticketu (po vytvoření zprávy)."""

    async def _run(tx):
        tickets = tx.get(HT_TICKETS_FILE, {})
        rec = tickets.get(str(channel_id))
        if rec is None:
            return None
        rec["panelMessageId"] = str(message_id)
        tx.set(HT_TICKETS_FILE, tickets)
        return rec

    return await store.transaction((HT_TICKETS_FILE,), _run)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
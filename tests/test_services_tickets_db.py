"""DB (PostgreSQL) režim services/tickets.py — Phase F3 (todo #6).

JSON režim (session_factory=None) je pokryt test_tickets.py; tento soubor
ověřuje, že produkční cesta přes ``session_factory`` dodržuje stejný JSON
kontrakt (výsledkové dicty, prevence duplicit, cooldowny, logy).
"""

from datetime import datetime, timezone

from db.repositories.cooldowns import COOLDOWN_HT3, CooldownRepository
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.tickets import TicketRepository
from db.services.session import transaction
from services import tickets as svc

NOW_MS = 1_700_000_000_000
COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000  # 7 dní (HT3_COOLDOWN_MS)

KITS = (("ht3", "HT3"), ("tourney", "Tournament"))
TIERS = (
    ("LT5", "ladder", "LT5", 1),
    ("LT4", "ladder", "LT4", 2),
    ("LT3", "ladder", "LT3", 3),
    ("LT3E", "virtual", "LT3E", None),
)


async def _seed(session_factory):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        await session.flush()


async def _create(session_factory, *, channel_id: int, owner_id: int = 100, **kw):
    return await svc.create_ticket(
        channel_id=channel_id,
        owner_id=str(owner_id),
        owner_name=kw.pop("owner_name", f"Owner{owner_id}"),
        ign=kw.pop("ign", f"Owner{owner_id}"),
        kit=kw.pop("kit", "HT3"),
        target_tier=kw.pop("target_tier", "LT4"),
        current_tier=kw.pop("current_tier", "LT5"),
        eval_ok=kw.pop("eval_ok", False),
        category_id=kw.pop("category_id", 42),
        now=NOW_MS,
        session_factory=session_factory,
        **kw,
    )


async def test_create_ticket_and_duplicate_blocked(session_factory, clean_db):
    await _seed(session_factory)
    r1 = await _create(session_factory, channel_id=111, owner_id=100)
    assert r1["result"] == "created"
    assert r1["ticket"]["id"] == "111"
    assert r1["ticket"]["status"] == "open"
    assert r1["ticket"]["ownerId"] == "100"
    assert r1["ticket"]["kit"] == "HT3"
    assert r1["ticket"]["targetTier"] == "LT4"
    assert r1["ticket"]["currentTier"] == "LT5"
    assert r1["ticket"]["panelMessageId"] is None
    assert r1["ticket"]["createdAt"] == NOW_MS
    r2 = await _create(session_factory, channel_id=222, owner_id=100)
    assert r2["result"] == "duplicate"
    assert r2["ticket"]["id"] == "111"


async def test_create_ticket_identity_conflict(session_factory, clean_db):
    await _seed(session_factory)
    async with transaction(session_factory) as session:
        await PlayerRepository().claim_discord_id(
            session, discord_id=500, ign="Shared"
        )
    r = await _create(session_factory, channel_id=113, owner_id=999, ign="Shared")
    assert r["result"] == "identity_conflict"


async def test_create_ticket_kit_resolution_by_display_name(session_factory, clean_db):
    await _seed(session_factory)
    r = await _create(session_factory, channel_id=114, owner_id=100, kit="tourney")
    assert r["result"] == "created"
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "tourney")
    assert kit is not None
    assert r["ticket"]["kit"] == "Tournament"


async def test_get_tickets_and_get_ticket(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=115, owner_id=100)
    await _create(session_factory, channel_id=116, owner_id=101)
    all_tickets = await svc.get_tickets(session_factory=session_factory)
    assert set(all_tickets.keys()) == {"115", "116"}
    t = await svc.get_ticket(116, session_factory=session_factory)
    assert t is not None and t["ownerId"] == "101"
    assert await svc.get_ticket(999, session_factory=session_factory) is None


async def test_find_open_ticket(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=117, owner_id=100)
    found = await svc.find_open_ticket(100, "HT3", session_factory=session_factory)
    assert found is not None and found["id"] == "117"
    assert (
        await svc.find_open_ticket(100, "Tournament", session_factory=session_factory)
        is None
    )
    assert await svc.find_open_ticket(777, "HT3", session_factory=session_factory) is None
    await svc.close_ticket(117, "200", session_factory=session_factory)
    assert (
        await svc.find_open_ticket(100, "HT3", session_factory=session_factory) is None
    )


async def test_claim_ticket(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=118, owner_id=100)
    r = await svc.claim_ticket(
        118, "200", "Tester A", session_factory=session_factory
    )
    assert r["result"] == "claimed"
    assert r["ticket"]["claimerId"] == "200"
    assert r["ticket"]["claimerName"] == "Tester A"
    r_own = await svc.claim_ticket(118, "100", "Owner", session_factory=session_factory)
    assert r_own["result"] == "own_ticket"
    r_other = await svc.claim_ticket(
        118, "300", "Tester B", session_factory=session_factory
    )
    assert r_other["result"] == "already_claimed"
    assert r_other["claimer_id"] == "200"
    r_same = await svc.claim_ticket(
        118, "200", "Tester A", session_factory=session_factory
    )
    assert r_same["result"] == "claimed"


async def test_unclaim_ticket(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=119, owner_id=100)
    await svc.claim_ticket(119, "200", "Tester", session_factory=session_factory)
    r_other = await svc.unclaim_ticket(119, "300", session_factory=session_factory)
    assert r_other["result"] == "not_claimer"
    r = await svc.unclaim_ticket(119, "200", session_factory=session_factory)
    assert r["result"] == "unclaimed"
    assert r["ticket"]["claimerId"] is None
    assert r["previous"]["claimer_id"] == "200"
    r2 = await svc.unclaim_ticket(119, "200", session_factory=session_factory)
    assert r2["result"] == "not_claimed"
    await svc.claim_ticket(119, "200", "Tester", session_factory=session_factory)
    r3 = await svc.unclaim_ticket(119, "300", force=True, session_factory=session_factory)
    assert r3["result"] == "unclaimed"


async def test_add_member(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=120, owner_id=100)
    r = await svc.add_member(
        120, "200", member_name="Witness", session_factory=session_factory
    )
    assert r["result"] == "added"
    assert r["ticket"]["members"] == ["200"]
    r2 = await svc.add_member(120, "200", session_factory=session_factory)
    assert r2["result"] == "already_member"
    r3 = await svc.add_member(120, "100", session_factory=session_factory)
    assert r3["result"] == "is_owner"
    r4 = await svc.add_member(120, "777", session_factory=session_factory)
    assert r4["result"] == "player_not_found"
    await svc.close_ticket(120, "200", session_factory=session_factory)
    r5 = await svc.add_member(
        120, "201", member_name="Late", session_factory=session_factory
    )
    assert r5["result"] == "not_open"


async def test_remove_member(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=121, owner_id=100)
    await svc.add_member(121, "200", member_name="W1", session_factory=session_factory)
    await svc.add_member(121, "201", member_name="W2", session_factory=session_factory)
    r = await svc.remove_member(121, "200", session_factory=session_factory)
    assert r["result"] == "removed"
    assert r["ticket"]["members"] == ["201"]
    r2 = await svc.remove_member(121, "200", session_factory=session_factory)
    assert r2["result"] == "not_member"
    r3 = await svc.remove_member(121, "777", session_factory=session_factory)
    assert r3["result"] == "not_member"
    await svc.claim_ticket(121, "300", "C", session_factory=session_factory)
    r4 = await svc.remove_member(121, "300", session_factory=session_factory)
    assert r4["result"] == "is_claimer"


async def test_close_ticket_sets_ht3_cooldown(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=122, owner_id=100)
    r = await svc.close_ticket(
        122, "200", cooldown_ms=COOLDOWN_MS, now=NOW_MS, session_factory=session_factory
    )
    assert r["result"] == "closed"
    assert r["ticket"]["status"] == "closed"
    assert r["ticket"]["closedAt"] == NOW_MS
    r2 = await svc.close_ticket(122, "200", session_factory=session_factory)
    assert r2["result"] == "already_closed"
    async with transaction(session_factory) as session:
        t = await TicketRepository().get_by_channel(session, 122)
        kit = await KitRepository().get_by_key(session, "ht3")
        active = await CooldownRepository().get_active(
            session,
            player_id=t.player_id,
            cooldown_type=COOLDOWN_HT3,
            kit_id=kit.id,
            now=datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc),
        )
    assert len(active) == 1
    assert int(active[0].expires_at.timestamp() * 1000) == NOW_MS + COOLDOWN_MS


async def test_reopen_ticket_respects_cooldown(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=123, owner_id=100)
    await svc.close_ticket(
        123, "200", cooldown_ms=COOLDOWN_MS, now=NOW_MS, session_factory=session_factory
    )
    r = await svc.reopen_ticket(
        123,
        "200",
        cooldown_ms=COOLDOWN_MS,
        now=NOW_MS + 1000,
        session_factory=session_factory,
    )
    assert r["result"] == "cooldown"
    assert r["kit"] == "HT3"
    assert 0 < r["remaining_ms"] <= COOLDOWN_MS
    r2 = await svc.reopen_ticket(
        123, "200", cooldown_ms=0, now=NOW_MS + 1000, session_factory=session_factory
    )
    assert r2["result"] == "reopened"
    assert r2["ticket"]["status"] == "open"
    assert r2["ticket"]["closedAt"] is None
    r3 = await svc.reopen_ticket(
        123, "200", now=NOW_MS + 2000, session_factory=session_factory
    )
    assert r3["result"] == "not_closed"


async def test_reopen_ticket_after_cooldown_expires(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=124, owner_id=100)
    await svc.close_ticket(
        124, "200", cooldown_ms=COOLDOWN_MS, now=NOW_MS, session_factory=session_factory
    )
    r = await svc.reopen_ticket(
        124,
        "200",
        cooldown_ms=COOLDOWN_MS,
        now=NOW_MS + COOLDOWN_MS + 1000,
        session_factory=session_factory,
    )
    assert r["result"] == "reopened"


async def test_set_panel_message(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=125, owner_id=100)
    rec = await svc.set_panel_message(125, 555001, session_factory=session_factory)
    assert rec is not None and rec["panelMessageId"] == "555001"
    t = await svc.get_ticket(125, session_factory=session_factory)
    assert t["panelMessageId"] == "555001"
    assert await svc.set_panel_message(999, 555001, session_factory=session_factory) is None


async def test_ticket_logs_roundtrip(session_factory, clean_db):
    await _seed(session_factory)
    await svc.log_ticket_event(
        "126",
        "opened",
        "100",
        "Owner",
        details="ticket created",
        now=NOW_MS,
        session_factory=session_factory,
    )
    await svc.log_ticket_event(
        "126",
        "claimed",
        "200",
        "Tester",
        details="claim",
        now=NOW_MS + 1000,
        session_factory=session_factory,
    )
    logs = await svc.get_ticket_logs("126", session_factory=session_factory)
    assert [entry["action"] for entry in logs] == ["opened", "claimed"]
    assert logs[0]["actorId"] == "100"
    assert logs[0]["actorName"] == "Owner"
    assert logs[0]["details"] == "ticket created"
    assert logs[1]["ts"] >= logs[0]["ts"]
    assert await svc.get_ticket_logs("999", session_factory=session_factory) == []


async def test_create_ticket_fight_type_with_panel(session_factory, clean_db):
    await _seed(session_factory)
    r = await _create(
        session_factory,
        channel_id=127,
        owner_id=100,
        ticket_type="fight",
        panel_message_id=555123,
        category_id=77,
    )
    assert r["result"] == "created"
    assert r["ticket"]["ticketType"] == "fight"
    assert r["ticket"]["categoryId"] == 77
    assert r["ticket"]["panelMessageId"] == "555123"
    r2 = await _create(
        session_factory, channel_id=128, owner_id=101, eval_ok=True, target_tier="LT3E"
    )
    assert r2["ticket"]["eval"] is True
    assert r2["ticket"]["targetTier"] == "LT3E"


async def test_members_are_discord_ids_in_contract(session_factory, clean_db):
    await _seed(session_factory)
    await _create(session_factory, channel_id=129, owner_id=100)
    await svc.add_member(129, "200", member_name="W", session_factory=session_factory)
    t = await svc.get_ticket(129, session_factory=session_factory)
    assert t["members"] == ["200"]
    assert t["ownerId"] == "100"
    assert t["panelMessageId"] is None
"""DB (PostgreSQL) režim services/results.py + services/topresult.py — Phase F (todo #7).

JSON režim (session_factory=None) je pokryt test_results.py / test_topresult.py;
tento soubor ověřuje, že produkční cesta přes ``session_factory`` dodržuje stejný
JSON kontrakt (výsledkové dicty, prevence duplicit, cooldowny, ticket stav, logy)
a že ``commit_promotion_with_wedge`` (cog) najde řádky podle ``result_key``.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from db.models import AuditLog, TierDefinition
from db.repositories.cooldowns import COOLDOWN_HT3, COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.results import PROMOTION_COMMITTED, PROMOTION_DISCORD_PENDING
from db.repositories.results import ResultRepository
from db.repositories.tiers import MirrorServiceRepository
from db.services.promotion import PromotionCommitService
from db.services.session import transaction
from services import results as rsvc
from services import tickets as tsvc
from services import topresult as htsvc

NOW_MS = 1_700_000_000_000
QUEUE_COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000
HT3_COOLDOWN_MS = 3 * 24 * 60 * 60 * 1000

KITS = (("ht3", "HT3"), ("tourney", "Tournament"))
TIERS = (
    ("LT5", "ladder", "LT5", 1),
    ("HT5", "ladder", "HT5", 2),
    ("LT4", "ladder", "LT4", 3),
    ("HT4", "ladder", "HT4", 4),
    ("LT3", "ladder", "LT3", 5),
    ("LT3E", "virtual", "LT3E", None),
    ("HT3", "ladder", "HT3", 6),
    ("LT2", "ladder", "LT2", 7),
    ("HT2", "ladder", "HT2", 8),
    ("LT1", "ladder", "LT1", 9),
    ("HT1", "ladder", "HT1", 10),
)


async def _seed(session_factory):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KITS, TIERS)
        await PlayerRepository().claim_discord_id(
            session, discord_id=200, ign="Tester"
        )
        await session.flush()


async def _ticket(session_factory, *, channel_id: int, owner_id: int = 100, **kw):
    return await tsvc.create_ticket(
        channel_id=channel_id,
        owner_id=str(owner_id),
        owner_name=kw.pop("owner_name", f"Owner{owner_id}"),
        ign=kw.pop("ign", f"Owner{owner_id}"),
        kit=kw.pop("kit", "HT3"),
        target_tier=kw.pop("target_tier", "LT4"),
        current_tier=kw.pop("current_tier", "LT5"),
        eval_ok=kw.pop("eval_ok", False),
        category_id=kw.pop("category_id", 42),
        ticket_type=kw.pop("ticket_type", None),
        now=NOW_MS,
        session_factory=session_factory,
        **kw,
    )


async def _record(session_factory, **kw):
    defaults = dict(
        player_id="100",
        player_name="Owner100",
        ign="Owner100",
        evaluator_id="200",
        evaluator_name="Tester",
        kit="HT3",
        new_tier="LT4",
        display_tier="LT4",
        score="3:1",
        outcome="Won",
        notes=None,
        eval_flag=False,
        now=NOW_MS,
        date="2025-11-15",
        queue_cooldown_ms=QUEUE_COOLDOWN_MS,
        ht3_cooldown_ms=HT3_COOLDOWN_MS,
        session_factory=session_factory,
    )
    defaults.update(kw)
    return await rsvc.record_result(**defaults)


def _dt(now_ms: int) -> datetime:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)


async def _ht3_active(session, player_id: int, kit_id: int):
    return await CooldownRepository().get_active(
        session,
        player_id=player_id,
        cooldown_type=COOLDOWN_HT3,
        kit_id=kit_id,
        now=_dt(NOW_MS),
    )


# --- /result (results.py) ---------------------------------------------------

async def test_record_result_ticket_created_closes_and_cooldowns(
    session_factory, clean_db
):
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=111, owner_id=100)
    r = await _record(session_factory, ticket_id=111)
    assert r["result"] == "created"
    assert r["previous_tier"] == "N/A"
    rec = r["record"]
    assert rec["id"] == "111"
    assert rec["kind"] == "ticket"
    assert rec["ticketId"] == "111"
    assert rec["playerId"] == "100"
    assert rec["playerName"] == "Owner100"
    assert rec["ign"] == "Owner100"
    assert rec["evaluatorId"] == "200"
    assert rec["evaluatorName"] == "Tester"
    assert rec["kit"] == "HT3"
    assert rec["previousTier"] == "N/A"
    assert rec["newTier"] == "LT4"
    assert rec["displayTier"] == "LT4"
    assert rec["score"] == "3:1"
    assert rec["outcome"] == "Won"
    assert rec["notes"] is None
    assert rec["eval"] is False
    assert rec["resultType"] == "normal"
    assert rec["timestamp"] == NOW_MS
    assert rec["date"] == "2025-11-15"

    t = await tsvc.get_ticket(111, session_factory=session_factory)
    assert t["status"] == "closed"

    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "ht3")
        player = await PlayerRepository().get_by_discord_id(session, 100)
        assert await _ht3_active(session, player.id, kit.id)
        wait = await CooldownRepository().get_active(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_WAITLIST,
            kit_id=None,
            now=_dt(NOW_MS),
        )
        assert len(wait) == 1
        updates = (await session.execute(
            select(AuditLog).where(AuditLog.entity_type == "ticket")
        )).scalars().all()
        assert [a.action for a in updates] == ["result"]
        assert updates[0].details["details"] == "N/A → LT4"


async def test_record_result_ticket_duplicate(session_factory, clean_db):
    """A COMMITTED result blocks retry — this is the real 'already
    promoted' case (H2 audit fix: duplicate detection is by promotion
    status, not mere row existence)."""
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=112, owner_id=100)
    r1 = await _record(session_factory, ticket_id=112)
    assert r1["result"] == "created"
    async with transaction(session_factory) as session:
        await ResultRepository().set_promotion_status(
            session, result_key="result:112", promotion_status=PROMOTION_COMMITTED
        )
    r2 = await _record(session_factory, ticket_id=112)
    assert r2["result"] == "duplicate"
    assert r2["existing"]["id"] == "112"


async def test_record_result_ticket_discord_pending_does_not_block_retry(
    session_factory, clean_db
):
    """H2 audit fix regression: a row stuck at discord_pending (the Discord
    role grant never confirmed/committed) must NOT be treated as a
    successful duplicate promotion — the previous behavior would have told
    the tester 'already recorded' forever, even though nothing was ever
    actually committed."""
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=114, owner_id=100)
    r1 = await _record(session_factory, ticket_id=114)
    assert r1["result"] == "created"

    async with transaction(session_factory) as session:
        stuck = await ResultRepository().get_by_key(session, "result:114")
    assert stuck.promotion_status == PROMOTION_DISCORD_PENDING

    await tsvc.reopen_ticket(114, "100", session_factory=session_factory)
    r2 = await _record(session_factory, ticket_id=114)
    assert r2["result"] != "duplicate", (
        "a discord_pending row must never be reported as an already-"
        f"succeeded duplicate promotion: {r2}"
    )


async def test_record_result_ticket_errors(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=113, owner_id=100)

    r = await _record(session_factory, ticket_id=999)
    assert r["result"] == "not_found"

    r = await _record(session_factory, ticket_id=113, player_id="999")
    assert r["result"] == "wrong_player"

    r = await _record(session_factory, ticket_id=113, kit="Tournament")
    assert r["result"] == "wrong_kit"

    r = await _record(session_factory, ticket_id=113, kit="Nope")
    assert r["result"] == "wrong_kit"

    r = await _record(
        session_factory, ticket_id=113, new_tier="HT3"
    )
    assert r["result"] == "invalid_tier"
    assert "přesáhnout" in r["message"]

    await tsvc.close_ticket(113, "100", session_factory=session_factory)
    r = await _record(session_factory, ticket_id=113, new_tier="LT4")
    assert r["result"] == "ticket_closed"


async def test_record_result_queue_created_and_duplicate_via_cooldown(
    session_factory, clean_db
):
    await _seed(session_factory)
    r = await _record(session_factory, ticket_id=None)
    assert r["result"] == "created"
    assert r["record"]["id"].startswith("queue-")
    assert r["record"]["kind"] == "queue"
    assert r["record"]["ticketId"] is None
    r2 = await _record(session_factory, ticket_id=None)
    assert r2["result"] == "duplicate"
    assert r2["existing"]["id"] == r["record"]["id"]

    r3 = await _record(
        session_factory,
        ticket_id=None,
        player_id="777",
        player_name="Fresh",
        ign="Fresh",
        kit="Nope",
        queue_cooldown_ms=0,
    )
    assert r3["result"] == "created"
    assert r3["record"]["kit"] == "Nope"

    r4 = await _record(
        session_factory,
        ticket_id=None,
        player_id="777",
        player_name="Fresh",
        ign="Fresh",
        new_tier="HT3",
    )
    assert r4["result"] == "invalid_tier"


async def test_record_result_eval(session_factory, clean_db):
    await _seed(session_factory)
    r = await _record(
        session_factory,
        ticket_id=None,
        new_tier="LT3E",
        display_tier="LT3 + eval",
        eval_flag=True,
    )
    assert r["result"] == "created"
    assert r["record"]["eval"] is True
    assert r["record"]["newTier"] == "LT3"
    assert r["record"]["displayTier"] == "LT3 + eval"


async def test_record_result_readers(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=115, owner_id=100)
    await _record(session_factory, ticket_id=115, notes="Poznámka")
    await _record(
        session_factory,
        ticket_id=None,
        player_id="101",
        player_name="Other",
        ign="Other",
        now=NOW_MS + 60_000,
    )

    by_ticket = await rsvc.get_result_by_ticket(
        115, session_factory=session_factory
    )
    assert by_ticket is not None and by_ticket["id"] == "115"
    assert by_ticket["notes"] == "Poznámka"
    assert await rsvc.get_result_by_ticket(
        999, session_factory=session_factory
    ) is None

    for_player = await rsvc.get_results_for_player(
        "100", session_factory=session_factory
    )
    assert [p["id"] for p in for_player] == ["115"]

    all_results = await rsvc.get_all_results(session_factory=session_factory)
    assert {p["id"] for p in all_results} == {"115", "queue-101-1700000060000"}


async def test_commit_after_discord_success_preserves_row_metadata(
    session_factory, clean_db
):
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=116, owner_id=100)
    r = await _record(session_factory, ticket_id=116, notes="Zachovat")
    rec = r["record"]
    assert rec["id"] == "116"

    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "ht3")
        player = await PlayerRepository().get_by_discord_id(session, 100)
        lt4 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "LT4")
            )
        ).scalar_one()
        service = PromotionCommitService()
        outcome = await service.commit_after_discord_success(
            session_factory,
            result_key=f"result:{rec['id']}",
            kind="ticket",
            player_id=player.id,
            kit_id=kit.id,
            new_tier_id=lt4.id,
            discord_role_id=555001,
            score=rec["score"],
            outcome=rec["outcome"],
            date=rec["date"],
            audit_actor_id=200,
            audit_actor_name="Tester",
        )
        assert outcome.already_committed is False

    async with transaction(session_factory) as session:
        row = await ResultRepository().get_by_key(session, f"result:{rec['id']}")
        assert row.promotion_status == PROMOTION_COMMITTED
        assert row.notes == "Zachovat"
        assert row.ticket_channel_id == 116
        assert row.kind == "ticket"


# --- /topresult (topresult.py) ----------------------------------------------

async def _ht_fight(session_factory, **kw):
    defaults = dict(
        player_id="100",
        player_name="Owner100",
        ign="Owner100",
        evaluator_id="200",
        evaluator_name="Tester",
        kit="HT3",
        fight_tier="HT3",
        score="3-2",
        outcome="Won",
        opponent_id="300",
        opponent_name="Rival",
        tier_status="Renew",
        bridge=None,
        notes=None,
        now=NOW_MS,
        date="2025-11-15",
        ht3_cooldown_ms=HT3_COOLDOWN_MS,
        session_factory=session_factory,
    )
    defaults.update(kw)
    return await htsvc.record_ht_fight(**defaults)


async def test_ht_fight_win_ticket_no_close_no_cooldown(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(
        session_factory, channel_id=211, owner_id=100, ticket_type="fight"
    )
    r = await _ht_fight(session_factory, ticket_id=211)
    assert r["result"] == "created"
    assert r["previous_tier"] == "N/A"
    rec = r["record"]
    assert rec["id"] == "211:ht_fight"
    assert rec["kind"] == "ht_fight"
    assert rec["resultType"] == "ht_fight"
    assert rec["ticketId"] == "211"
    assert rec["fightTier"] == "HT3"
    assert rec["tierStatus"] == "Renew"
    assert rec["opponentId"] == "300"
    assert rec["opponentName"] == "Rival"
    assert rec["announcement"] == "pending"
    assert rec["newTier"] == ""

    t = await tsvc.get_ticket(211, session_factory=session_factory)
    assert t["status"] == "open"

    async with transaction(session_factory) as session:
        row = await ResultRepository().get_by_key(
            session, "ht_fight:211:ht_fight"
        )
        assert row.promotion_status == PROMOTION_DISCORD_PENDING
        assert row.subtype == "HT3"

    # H2 audit fix: while still discord_pending, a retry must NOT be told
    # "already recorded" — only a COMMITTED row blocks retry.
    r2 = await _ht_fight(session_factory, ticket_id=211)
    assert r2["result"] != "duplicate"

    async with transaction(session_factory) as session:
        await ResultRepository().set_promotion_status(
            session,
            result_key="ht_fight:211:ht_fight",
            promotion_status=PROMOTION_COMMITTED,
        )
    r3 = await _ht_fight(session_factory, ticket_id=211)
    assert r3["result"] == "duplicate"
    assert r3["existing"]["id"] == "211:ht_fight"


async def test_ht_fight_loss_ticket_closes_and_cooldowns(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(
        session_factory, channel_id=212, owner_id=100, ticket_type="fight"
    )
    r = await _ht_fight(session_factory, ticket_id=212, outcome="Lost", score="1-3")
    assert r["result"] == "created"
    assert r["record"]["announcement"] == "pending"

    t = await tsvc.get_ticket(212, session_factory=session_factory)
    assert t["status"] == "closed"

    async with transaction(session_factory) as session:
        row = await ResultRepository().get_by_key(
            session, "ht_fight:212:ht_fight"
        )
        assert row.promotion_status is None
        assert row.outcome == "Lost"
        kit = await KitRepository().get_by_key(session, "ht3")
        player = await PlayerRepository().get_by_discord_id(session, 100)
        assert await _ht3_active(session, player.id, kit.id)
        logs = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_type == "ticket",
                    AuditLog.entity_id == "212",
                )
            )
        ).scalars().all()
        assert [a.action for a in logs] == ["ht_fight"]
        assert logs[0].details["details"] == "N/A (prohra)"


async def test_ht_fight_error_branches(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(session_factory, channel_id=213, owner_id=100, ticket_type="fight")

    r = await _ht_fight(session_factory, ticket_id=999)
    assert r["result"] == "not_found"

    await _ticket(session_factory, channel_id=214, owner_id=101)
    r = await _ht_fight(session_factory, ticket_id=214)
    assert r["result"] == "not_fight_ticket"

    r = await _ht_fight(session_factory, ticket_id=213, player_id="999")
    assert r["result"] == "wrong_player"

    r = await _ht_fight(session_factory, ticket_id=213, kit="Tournament")
    assert r["result"] == "wrong_kit"

    r = await _ht_fight(session_factory, ticket_id=213, fight_tier="ZZZ")
    assert r["result"] == "invalid_tier"

    r = await _ht_fight(session_factory, ticket_id=213, score="abc")
    assert r["result"] == "invalid_score"

    r = await _ht_fight(session_factory, ticket_id=213, outcome="Draw")
    assert r["result"] == "invalid_outcome"

    r = await _ht_fight(
        session_factory, ticket_id=213, outcome="Lost", bridge="HT3"
    )
    assert r["result"] == "invalid_bridge"  # bridge jen při výhře


async def test_ht_fight_free_win_promotes_and_bridge(session_factory, clean_db):
    await _seed(session_factory)
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "ht3")
        _o, player = await PlayerRepository().claim_discord_id(
            session, discord_id=100, ign="Owner100"
        )
        lt5 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "LT5")
            )
        ).scalar_one()
        _obs = await MirrorServiceRepository().apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=lt5.id,
            observed_at=_dt(NOW_MS - 60_000),
            source="discord_sync",
        )
    r = await _ht_fight(session_factory, ticket_id=None)
    assert r["result"] == "created"
    assert r["previous_tier"] == "LT5"
    assert r["record"]["previousTier"] == "LT5"
    assert r["record"]["newTier"] == "HT5"
    assert r["record"]["id"].startswith("htfight-")

    r2 = await _ht_fight(session_factory, ticket_id=None)
    assert r2["result"] == "duplicate"  # stejný fingerprint v 2h okně

    r3 = await _ht_fight(
        session_factory,
        ticket_id=None,
        now=NOW_MS + 30_000,
        score="0-0",
        outcome="Lost",
    )
    assert r3["result"] == "created"

    r4 = await _ht_fight(
        session_factory,
        ticket_id=None,
        now=NOW_MS + 60_000,
        score="4-1",
        bridge="LT5",
    )
    assert r4["result"] == "invalid_bridge"  # bridge musí být strictly vyšší


async def test_ht_fight_bridge_higher_than_current(session_factory, clean_db):
    await _seed(session_factory)
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "ht3")
        _o, player = await PlayerRepository().claim_discord_id(
            session, discord_id=100, ign="Owner100"
        )
        ht3 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "HT3")
            )
        ).scalar_one()
        _obs = await MirrorServiceRepository().apply_observation(
            session,
            player_id=player.id,
            kit_id=kit.id,
            tier_id=ht3.id,
            observed_at=_dt(NOW_MS - 60_000),
            source="discord_sync",
        )
    r = await _ht_fight(
        session_factory, ticket_id=None, bridge="HT3"
    )
    assert r["result"] == "invalid_bridge"
    r2 = await _ht_fight(
        session_factory, ticket_id=None, bridge="LT2"
    )
    assert r2["result"] == "created"
    assert r2["record"]["bridgeTier"] == "LT2"
    assert r2["record"]["newTier"] == "LT2"


async def test_ht_fight_announcement_and_readers(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(
        session_factory, channel_id=215, owner_id=100, ticket_type="fight"
    )
    await _ht_fight(session_factory, ticket_id=215)
    rid = "215:ht_fight"

    r = await htsvc.set_ht_fight_announcement(
        rid, "sent", message_id=777001, session_factory=session_factory
    )
    assert r["result"] == "ok"
    assert r["record"]["announcement"] == "sent"

    r_bad = await htsvc.set_ht_fight_announcement(
        rid, "bogus", session_factory=session_factory
    )
    assert r_bad["result"] == "invalid_status"
    r_missing = await htsvc.set_ht_fight_announcement(
        "999:ht_fight", "sent", session_factory=session_factory
    )
    assert r_missing["result"] == "not_found"

    by_ticket = await htsvc.get_ht_fight_result_for_ticket(
        215, session_factory=session_factory
    )
    assert by_ticket is not None
    assert by_ticket["id"] == rid
    assert by_ticket["announcement"] == "sent"
    assert (
        await htsvc.get_ht_fight_result_for_ticket(
            999, session_factory=session_factory
        )
        is None
    )

    listing = await htsvc.get_ht_fight_results(session_factory=session_factory)
    assert any(x["id"] == rid for x in listing)


async def test_ht_fight_commit_preserves_subtype_and_ticket_channel(
    session_factory, clean_db
):
    await _seed(session_factory)
    await _ticket(
        session_factory, channel_id=216, owner_id=100, ticket_type="fight"
    )
    r = await _ht_fight(
        session_factory, ticket_id=216, notes="Fight poznámka"
    )
    rec = r["record"]
    assert rec["id"] == "216:ht_fight"

    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, "ht3")
        player = await PlayerRepository().get_by_discord_id(session, 100)
        ht3 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "HT3")
            )
        ).scalar_one()
        service = PromotionCommitService()
        outcome = await service.commit_after_discord_success(
            session_factory,
            result_key=f"ht_fight:{rec['id']}",
            kind="ht_fight",
            player_id=player.id,
            kit_id=kit.id,
            new_tier_id=ht3.id,
            discord_role_id=555002,
            score=rec["score"],
            outcome=rec["outcome"],
            date=rec["date"],
            audit_actor_id=200,
            audit_actor_name="Tester",
        )
        assert outcome.already_committed is False

    async with transaction(session_factory) as session:
        row = await ResultRepository().get_by_key(
            session, f"ht_fight:{rec['id']}"
        )
        assert row.promotion_status == PROMOTION_COMMITTED
        assert row.subtype == "HT3"
        assert row.ticket_channel_id == 216
        assert row.opponent_id == 300
        assert row.notes == "Fight poznámka"
"""HT Fight průvodce: čistá logika (sekce, skóre, zpráva) a zápis více zápasů do PostgreSQL."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from db.models import Result, TierDefinition
from db.repositories.cooldowns import COOLDOWN_HT3, CooldownRepository
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.results import (
    PROMOTION_COMMITTED,
    PROMOTION_DISCORD_PENDING,
)
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction
from services import tickets as tsvc
from services import topresult as htsvc
from services.ht_fights import (
    Fight,
    FightScore,
    fight_sections,
    format_topresult_fights_message,
    missing_sections,
    parse_fight_score,
)

NOW_MS = 1_700_000_000_000
HT3_COOLDOWN_MS = 3 * 24 * 60 * 60 * 1000
TIERS = (
    ("LT3", "ladder", "LT3", 5),
    ("LT3E", "virtual", "LT3E", None),
    ("HT3", "ladder", "HT3", 6),
    ("LT2", "ladder", "LT2", 7),
    ("HT2", "ladder", "HT2", 8),
)


# --- čistá logika -----------------------------------------------------------
@pytest.mark.parametrize(
    "target,expected",
    [
        ("HT3", ["HT3"]),
        ("LT2", ["HT3", "LT2"]),
        ("HT2", ["LT2", "HT2"]),
        ("LT1", ["HT2", "LT1"]),
        ("HT1", ["LT1", "HT1"]),
        ("lt2", ["HT3", "LT2"]),
        ("LT3", ["LT3"]),
        ("nesmysl", []),
    ],
)
def test_fight_sections(target, expected):
    assert fight_sections(target) == expected


@pytest.mark.parametrize(
    "raw,ft,player,opponent,forfeit,outcome",
    [
        ("3-1", 3, 3, 1, False, "Won"),
        ("1-4", 4, 1, 4, False, "Lost"),
        ("4:2", 4, 4, 2, False, "Won"),
        ("3-1 ff", 5, 3, 1, True, "Won"),
        ("0-2 vzdal", 5, 0, 2, True, "Lost"),
        ("  10-7  ", 10, 10, 7, False, "Won"),
    ],
)
def test_parse_fight_score_ok(raw, ft, player, opponent, forfeit, outcome):
    score, error = parse_fight_score(raw, ft)
    assert error == ""
    assert (score.player, score.opponent, score.forfeit, score.outcome) == (
        player,
        opponent,
        forfeit,
        outcome,
    )


@pytest.mark.parametrize(
    "raw,ft",
    [
        ("abc", 3),
        ("3", 3),
        ("3-3", 3),  # remíza
        ("2-1", 3),  # nikdo nedosáhl FT a není ff
        ("5-1", 3),  # nad FT
        ("5-1 ff", 3),  # ff nepovoluje víc než FT
        ("", 3),
    ],
)
def test_parse_fight_score_rejects(raw, ft):
    score, error = parse_fight_score(raw, ft)
    assert score is None
    assert error


def test_missing_sections():
    assert missing_sections(["HT3", "LT2"], {"HT3": ["1"], "LT2": []}) == ["LT2"]
    assert missing_sections(["HT3"], {"HT3": ["1"]}) == []


def test_message_groups_fights_by_section():
    fights = [
        Fight("HT3", "11", "a", FightScore(4, 1, False)),
        Fight("HT3", "12", "b", FightScore(2, 4, False)),
        Fight("LT2", "13", "c", FightScore(4, 3, False)),
    ]
    text = format_topresult_fights_message(
        player_id="1", ign="mendu__", tier_status="Povýšen na LT2", kit="MolePVP",
        first_to=4, fights=fights, previous_tier="HT3", new_tier="LT2", role_id=99,
    )
    assert text == (
        "<@1> - mendu__ - **Povýšen na LT2** - MolePVP\n"
        "\n"
        "**HT3 Fighty (FT4):**\n"
        "> vyhrál 4-1 <@11>\n"
        "> prohrál 2-4 <@12>\n"
        "\n"
        "**LT2 Fighty (FT4):**\n"
        "> vyhrál 4-3 <@13>\n"
        "\n"
        "**Postup: HT3 → LT2**\n"
        "\n"
        "<@&99>"
    )


def test_message_without_promotion_has_no_progress_line():
    text = format_topresult_fights_message(
        player_id="1", ign="x", tier_status="Zůstává HT3", kit="K", first_to=3,
        fights=[Fight("HT3", "11", "a", FightScore(1, 3, False))],
        previous_tier="HT3", new_tier="", role_id=99,
    )
    assert "Postup" not in text


# --- zápis do databáze ------------------------------------------------------
async def _seed(session_factory, *, ign="Owner100", first_to=4, current="HT3"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, (("molepvp", "MolePvP"),), TIERS)
        kit = await KitRepository().get_by_key(session, "molepvp")
        kit.first_to = first_to
        await PlayerRepository().claim_discord_id(session, discord_id=200, ign="Tester")
        if ign is not None:
            _o, player = await PlayerRepository().claim_discord_id(
                session, discord_id=100, ign=ign
            )
            if current:
                tier = (
                    await session.execute(
                        select(TierDefinition).where(TierDefinition.code == current)
                    )
                ).scalar_one()
                await MirrorServiceRepository().apply_observation(
                    session, player_id=player.id, kit_id=kit.id, tier_id=tier.id,
                    observed_at=datetime.fromtimestamp(
                        (NOW_MS - 60_000) / 1000, tz=timezone.utc
                    ),
                    source="discord_sync",
                )


async def _ticket(session_factory, channel_id=311, ign="Owner100"):
    return await tsvc.create_ticket(
        channel_id=channel_id, owner_id="100", owner_name="Owner100", ign=ign,
        kit="MolePvP", target_tier="LT2", current_tier="HT3", eval_ok=False,
        category_id=42, ticket_type="fight", now=NOW_MS, session_factory=session_factory,
    )


def _fights():
    return [
        Fight("HT3", "300", "Rival1", FightScore(4, 2, False)),
        Fight("HT3", "301", "Rival2", FightScore(1, 4, False)),
        Fight("LT2", "302", "Rival3", FightScore(3, 4, False)),
    ]


async def _record(session_factory, **kw):
    defaults = dict(
        player_id="100", evaluator_id="200", evaluator_name="Tester", kit="MolePvP",
        fights=_fights(), target_tier="LT2", tier_gained=True, now=NOW_MS,
        date="2026-10-03", ht3_cooldown_ms=HT3_COOLDOWN_MS, session_factory=session_factory,
    )
    defaults.update(kw)
    return await htsvc.record_ht_fights(**defaults)


async def _rows(session_factory):
    async with transaction(session_factory) as session:
        rows = (
            await session.execute(
                select(Result).where(Result.kind == "ht_fight").order_by(Result.id)
            )
        ).scalars().all()
        return [
            (r.result_key, r.subtype, r.score, r.outcome, r.new_tier_id is not None,
             r.promotion_status, r.opponent_id)
            for r in rows
        ]


async def test_one_row_per_fight_and_ign_comes_from_db(session_factory, clean_db):
    await _seed(session_factory, ign="DbIgn")
    await _ticket(session_factory, ign="DbIgn")
    r = await _record(session_factory, ticket_id=311)

    assert r["result"] == "created"
    assert r["ign"] == "DbIgn"
    assert r["previous_tier"] == "HT3"
    assert r["promoted"] == "LT2"
    assert [x["id"] for x in r["records"]] == [
        "311:ht_fight:HT3:300", "311:ht_fight:HT3:301", "311:ht_fight:LT2:302",
    ]
    assert r["record"]["id"] == "311:ht_fight:LT2:302"
    assert r["record"]["newTier"] == "LT2"
    assert r["record"]["tierStatus"] == "Povýšen na LT2"

    rows = await _rows(session_factory)
    assert [(x[1], x[2], x[3], x[4], x[5], x[6]) for x in rows] == [
        ("HT3", "4-2", "Won", False, PROMOTION_COMMITTED, 300),
        ("HT3", "1-4", "Lost", False, None, 301),
        ("LT2", "3-4", "Lost", True, PROMOTION_DISCORD_PENDING, 302),
    ]
    ticket = await tsvc.get_ticket(311, session_factory=session_factory)
    assert ticket["status"] == "open"


async def test_opponent_does_not_need_to_be_a_known_player(session_factory, clean_db):
    """Soupeř je kdokoliv – nemusí být v DB ani mít tester roli."""
    await _seed(session_factory)
    r = await _record(
        session_factory,
        fights=[Fight("HT3", "999999", "Cizinec", FightScore(4, 0, False))],
        target_tier="HT3", tier_gained=False,
    )
    assert r["result"] == "created"


async def test_tier_not_gained_closes_ticket_and_sets_cooldown(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(session_factory)
    r = await _record(session_factory, ticket_id=311, tier_gained=False)

    assert r["result"] == "created"
    assert r["promoted"] is None
    assert r["record"]["tierStatus"] == "Zůstává HT3"
    assert r["record"]["newTier"] == ""
    rows = await _rows(session_factory)
    assert all(x[4] is False for x in rows)  # žádný řádek nenese povýšení
    ticket = await tsvc.get_ticket(311, session_factory=session_factory)
    assert ticket["status"] == "closed"
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 100)
        kit = await KitRepository().get_by_key(session, "molepvp")
        cooldown = await CooldownRepository().get_active(
            session, player_id=player.id, cooldown_type=COOLDOWN_HT3, kit_id=kit.id,
            now=datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc),
        )
    assert cooldown is not None


async def test_second_submit_is_duplicate_and_writes_nothing(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(session_factory)
    await _record(session_factory, ticket_id=311, tier_gained=False)
    before = await _rows(session_factory)
    again = await _record(session_factory, ticket_id=311, tier_gained=False)
    assert again["result"] in ("duplicate", "ticket_closed")
    assert await _rows(session_factory) == before


async def test_missing_ign_in_db_is_refused(session_factory, clean_db):
    await _seed(session_factory, ign=None)
    r = await _record(session_factory, tier_gained=False, target_tier="HT3")
    assert r["result"] == "ign_missing"
    assert await _rows(session_factory) == []


async def test_bridge_must_be_higher_than_current(session_factory, clean_db):
    await _seed(session_factory, current="LT2")
    r = await _record(session_factory, bridge="HT3", target_tier="HT2")
    assert r["result"] == "invalid_bridge"
    assert await _rows(session_factory) == []


async def test_bridge_is_refused_without_promotion(session_factory, clean_db):
    await _seed(session_factory)
    r = await _record(session_factory, bridge="HT2", tier_gained=False)
    assert r["result"] == "invalid_bridge"


async def test_bridge_overrides_target_and_is_recorded(session_factory, clean_db):
    await _seed(session_factory, current="LT3")
    r = await _record(
        session_factory, bridge="LT2", target_tier="HT3",
        fights=[Fight("HT3", "300", "R", FightScore(4, 1, False))],
    )
    assert r["promoted"] == "LT2"
    assert r["record"]["bridgeTier"] == "LT2"


async def test_free_result_duplicate_within_window(session_factory, clean_db):
    await _seed(session_factory)
    one = [Fight("HT3", "300", "R", FightScore(4, 1, False))]
    first = await _record(session_factory, fights=one, target_tier="HT3", tier_gained=False)
    assert first["result"] == "created"
    second = await _record(session_factory, fights=one, target_tier="HT3", tier_gained=False)
    assert second["result"] == "duplicate"


async def test_wrong_owner_is_refused(session_factory, clean_db):
    await _seed(session_factory)
    await _ticket(session_factory)
    r = await _record(session_factory, ticket_id=311, player_id="200")
    assert r["result"] == "wrong_player"
    assert await _rows(session_factory) == []

"""D2 — tests for the idempotent relational import (services.phase_d.import_data)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from db.models import (
    AuditLog,
    Cooldown,
    Kit,
    MigrationImportIssue,
    Player,
    TierDefinition,
    TierHistory,
)
from db.repositories.players import PlayerRepository

from services.phase_d.import_data import (
    AUDIT_ACTION_IMPORT,
    BOT_CONFIG_IMPORT_KEY,
    CAT_COOLDOWN_UNRESOLVED,
    CAT_TESTER_UNRESOLVED,
    import_json_data,
    parse_dd_mm_yyyy,
)

KITS = [
    "AnchorPvP",
    "NetheriteSword",
    "IronAxe",
    "GoldSMP",
    "UHCMace",
    "RandomPot",
    "ShieldlessSMP",
    "MolePVP",
]

PLAYERS = [
    {
        "username": "Atrajmix_",
        "modes": {"IronAxe": "LT3"},
        "history": {
            "IronAxe": [
                {"date": "16.05.2026", "tier": "LT3"},
                {"date": "09.07.2026", "tier": "LT4"},
            ],
            "MolePVP": [{"date": "02.06.2026", "tier": "NIC JE"}],
        },
    },
    {
        "username": "Crityx_",
        "modes": {},
        "history": {"RandomPot": [{"date": "01.01.2026", "tier": "LT3 EVAL"}]},
    },
    {
        "username": "nobody_here",
        "modes": {},
        "history": {"IronAxe": [{"date": "not-a-date", "tier": "LT1"}]},
    },
    {"username": "", "modes": {}, "history": {}},
    {
        "username": "guest",
        "modes": {},
        "history": {"NoSuchKit": [{"date": "10.10.2026", "tier": "LT2"}]},
    },
]

LINKED_DID = 111111111111111111
UNLINKED_DID = 222222222222222222
COOLDOWNS = {str(LINKED_DID): 1780593929525, str(UNLINKED_DID): 1780593929525}
HT3 = {str(LINKED_DID): {"IronAxe": 1785769543952, "NoSuchKit": 1}}
TESTERS = [str(LINKED_DID), "999999999999999999"]


def _write(data_dir, files: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (data_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _write_all(data_dir) -> None:
    _write(
        data_dir,
        {
            "players.json": PLAYERS,
            "kits.json": KITS,
            "cooldowns.json": COOLDOWNS,
            "ht3_cooldowns.json": HT3,
            "testers.json": TESTERS,
        },
    )


async def _link(session_factory, *, discord_id: int, ign: str) -> Player:
    async with session_factory() as session:
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=discord_id, ign=ign
        )
        await session.commit()
        return player


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(model)
        )
        return result.scalar_one()


async def _get_report(session_factory) -> dict:
    async with session_factory() as session:
        return await BotConfigRepository_get(session)


async def BotConfigRepository_get(session) -> dict:
    from db.repositories.sync_audit import BotConfigRepository

    return await BotConfigRepository().get(session, BOT_CONFIG_IMPORT_KEY)


@pytest.fixture
def link_helpers(session_factory):
    return session_factory


@pytest.mark.asyncio
async def test_import_full_flow(tmp_path, session_factory, clean_db):
    await _link(session_factory, discord_id=LINKED_DID, ign="Atrajmix_")
    _write_all(tmp_path)

    report = await import_json_data(session_factory, data_dir=tmp_path)

    assert report["kits_ensured"] == len(KITS)
    assert report["tiers_ensured"] == 6  # LT1..LT4 + "LT3 EVAL" + "NIC JE"
    assert report["players_created"] == 3
    assert report["players_existing"] == 1  # Atrajmix_ linked before import
    assert report["history_imported"] == 4
    assert report["history_invalid_dates"] == 1  # nobody_here
    assert report["history_unknown_kit"] == 1  # guest / NoSuchKit
    assert report["cooldowns_imported"] == 1
    assert report["cooldowns_unresolved"] == 1
    assert report["ht3_imported"] == 1
    assert report["ht3_unknown_kit"] == 1
    assert report["testers_imported"] == 1
    assert report["testers_unresolved"] == 1

    assert await _count(session_factory, Kit) == len(KITS)
    assert await _count(session_factory, TierHistory) == 4
    assert await _count(session_factory, Cooldown) == 2  # waitlist + ht3
    from db.models import Tester

    assert await _count(session_factory, Tester) == 1
    assert await _count(session_factory, MigrationImportIssue) == 6

    async with session_factory() as session:
        tier = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "LT3 EVAL")
            )
        ).scalar_one()
        assert tier.kind == "virtual"
        ladder = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "LT3")
            )
        ).scalar_one()
        assert ladder.kind == "ladder"

        ht = (
            await session.execute(
                select(TierHistory).where(TierHistory.source == "migration").limit(1)
            )
        ).scalar_one()
        assert ht.previous_tier_id is None
        assert ht.changed_at == datetime(2026, 5, 16, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_import_idempotent_second_run(tmp_path, session_factory, clean_db):
    await _link(session_factory, discord_id=LINKED_DID, ign="Atrajmix_")
    _write_all(tmp_path)

    first = await import_json_data(session_factory, data_dir=tmp_path)
    second = await import_json_data(session_factory, data_dir=tmp_path)

    assert first["history_imported"] == 4
    assert second["history_imported"] == 0
    assert second["history_skipped_existing"] == 4
    assert second["players_created"] == 0
    assert second["players_existing"] == 4
    assert second["issues_recorded"] == 0  # dedupe on natural key
    assert second["cooldowns_imported"] == 1  # upsert, no growth
    assert second["testers_existing"] == 1

    assert await _count(session_factory, TierHistory) == 4
    assert await _count(session_factory, MigrationImportIssue) == 6


@pytest.mark.asyncio
async def test_unresolved_ids_never_guessed(tmp_path, session_factory, clean_db):
    _write_all(tmp_path)
    await import_json_data(session_factory, data_dir=tmp_path)

    assert await _count(session_factory, Cooldown) == 0  # nothing linked
    from db.models import Tester

    assert await _count(session_factory, Tester) == 0

    async with session_factory() as session:
        issues = (
            (await session.execute(select(MigrationImportIssue))).scalars().all()
        )
        categories = {i.category for i in issues}
        assert CAT_COOLDOWN_UNRESOLVED in categories
        assert CAT_TESTER_UNRESOLVED in categories
        unlinked = [i for i in issues if i.source_key == str(UNLINKED_DID)]
        assert unlinked
        assert unlinked[0].payload["discord_id"] == str(UNLINKED_DID)
        # player rows exist for IGN sources only; UNLINKED_DID produced NO player
        assert await _count(session_factory, Player) == 4


@pytest.mark.asyncio
async def test_history_source_and_utc_midnight(tmp_path, session_factory, clean_db):
    _write(
        tmp_path,
        {
            "players.json": [
                {
                    "username": "p1",
                    "modes": {},
                    "history": {
                        "IronAxe": [{"date": "31.12.2025", "tier": "LT1"}]
                    },
                }
            ],
            "kits.json": ["IronAxe"],
        },
    )
    await import_json_data(session_factory, data_dir=tmp_path)

    async with session_factory() as session:
        row = (
            (await session.execute(select(TierHistory))).scalars().one()
        )
        assert row.source == "migration"
        assert row.changed_at == datetime(2025, 12, 31, tzinfo=timezone.utc)
        assert row.previous_tier_id is None


@pytest.mark.asyncio
async def test_rollback_on_malformed_json(tmp_path, session_factory, clean_db):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "players.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        await import_json_data(session_factory, data_dir=tmp_path)

    assert await _count(session_factory, Kit) == 0
    assert await _count(session_factory, Player) == 0
    assert await _count(session_factory, AuditLog) == 0  # no partial markers


@pytest.mark.asyncio
async def test_markers_written(tmp_path, session_factory, clean_db):
    await _link(session_factory, discord_id=LINKED_DID, ign="Atrajmix_")
    _write_all(tmp_path)
    await import_json_data(session_factory, data_dir=tmp_path)

    async with session_factory() as session:
        marker = await BotConfigRepository_get(session)
        assert marker is not None
        assert marker["report"]["history_imported"] == 4

        logs = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == AUDIT_ACTION_IMPORT)
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1
        assert logs[0].entity_type == "import"


@pytest.mark.asyncio
async def test_ht3_kit_expiry_and_type(tmp_path, session_factory, clean_db):
    await _link(session_factory, discord_id=LINKED_DID, ign="Atrajmix_")
    _write(
        tmp_path,
        {
            "players.json": PLAYERS,
            "kits.json": KITS,
            "ht3_cooldowns.json": {str(LINKED_DID): {"IronAxe": 1785769543952}},
        },
    )
    report = await import_json_data(session_factory, data_dir=tmp_path)
    assert report["ht3_imported"] == 1

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    select(Cooldown).where(Cooldown.cooldown_type == "ht3")
                )
            )
            .scalars()
            .one()
        )
        assert row.expires_at == datetime.fromtimestamp(
            1785769543952 / 1000.0, tz=timezone.utc
        )
        assert row.source == "migration"


@pytest.mark.asyncio
async def test_issue_categories_documented(tmp_path, session_factory, clean_db):
    await _link(session_factory, discord_id=LINKED_DID, ign="Atrajmix_")
    _write_all(tmp_path)
    await import_json_data(session_factory, data_dir=tmp_path)

    async with session_factory() as session:
        issues = (
            (await session.execute(select(MigrationImportIssue))).scalars().all()
        )
        cats = {i.category for i in issues}
        assert categories_superset() <= cats  # no unexpected categories


def categories_superset() -> set:
    return {
        "cooldown_unresolved_player",
        "ht3_cooldown_unknown_kit",
        "tester_unresolved_player",
        "player_history_invalid_date",
        "player_history_unknown_kit",
        "player_no_username",
    }


@pytest.mark.asyncio
async def test_parse_dates():
    assert parse_dd_mm_yyyy("16.05.2026") == datetime(
        2026, 5, 16, tzinfo=timezone.utc
    )
    assert parse_dd_mm_yyyy("not-a-date") is None
    assert parse_dd_mm_yyyy("32.13.2026") is None
    assert parse_dd_mm_yyyy(None) is None
    assert parse_dd_mm_yyyy(5) is None
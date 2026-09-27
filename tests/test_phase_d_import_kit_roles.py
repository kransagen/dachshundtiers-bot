"""D2b — tests for the idempotent kit_roles.json import.

kit_roles.json -> kit_roles table is a real gap left by D2's import_data.py
(which only imports players/tiers/history/cooldowns): a fresh PostgreSQL
cutover starts with an empty kit_roles table, and the bot refuses to start
on zero mappings (db.services.config_validation). This importer closes it.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from db.models import Kit, KitRole, TierDefinition
from services.phase_d.import_kit_roles import import_kit_roles_json


def _write(data_dir, mapping: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "kit_roles.json").write_text(
        json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
    )


async def test_import_creates_kits_tiers_and_mappings(tmp_path, session_factory, clean_db):
    _write(
        tmp_path,
        {
            "randompot": {"HT3": "111", "LT2": "222"},
            "ironaxe": {"HT1": "333"},
        },
    )

    report = await import_kit_roles_json(session_factory, data_dir=tmp_path)

    assert report["kits_seen"] == 2
    assert report["mappings_imported"] == 3
    assert report["skipped_invalid"] == []

    async with session_factory() as session:
        kits = {k.key for k in (await session.execute(select(Kit))).scalars()}
        tiers = {t.code for t in (await session.execute(select(TierDefinition))).scalars()}
        roles = list((await session.execute(select(KitRole))).scalars())

    assert kits == {"randompot", "ironaxe"}
    assert tiers == {"HT3", "LT2", "HT1"}
    assert {r.discord_role_id for r in roles} == {111, 222, 333}


async def test_import_idempotent_second_run(tmp_path, session_factory, clean_db):
    _write(tmp_path, {"randompot": {"HT3": "111"}})

    first = await import_kit_roles_json(session_factory, data_dir=tmp_path)
    second = await import_kit_roles_json(session_factory, data_dir=tmp_path)

    assert first["mappings_imported"] == 1
    assert second["mappings_imported"] == 1

    async with session_factory() as session:
        roles = list((await session.execute(select(KitRole))).scalars())
    assert len(roles) == 1
    assert roles[0].discord_role_id == 111


async def test_import_updates_role_id_on_rerun(tmp_path, session_factory, clean_db):
    """A kit/tier remapped to a new role id (re-run /setkitrole) must update,
    not duplicate, the existing row."""
    _write(tmp_path, {"randompot": {"HT3": "111"}})
    await import_kit_roles_json(session_factory, data_dir=tmp_path)

    _write(tmp_path, {"randompot": {"HT3": "999"}})
    await import_kit_roles_json(session_factory, data_dir=tmp_path)

    async with session_factory() as session:
        roles = list((await session.execute(select(KitRole))).scalars())
    assert len(roles) == 1
    assert roles[0].discord_role_id == 999


async def test_non_numeric_role_id_is_skipped_not_crashed(tmp_path, session_factory, clean_db):
    _write(tmp_path, {"randompot": {"HT3": "not-a-number", "LT2": "222"}})

    report = await import_kit_roles_json(session_factory, data_dir=tmp_path)

    assert report["mappings_imported"] == 1
    assert len(report["skipped_invalid"]) == 1
    assert report["skipped_invalid"][0]["tier"] == "HT3"


async def test_missing_file_imports_nothing(tmp_path, session_factory, clean_db):
    report = await import_kit_roles_json(session_factory, data_dir=tmp_path)
    assert report["kits_seen"] == 0
    assert report["mappings_imported"] == 0

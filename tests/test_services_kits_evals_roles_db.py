"""DB (PostgreSQL) i JSON režim services/kit_catalog, evals, kit_roles — Phase F (todo #9).

Ověřuje dual-mode kontrakt tří služeb, které nahrazují legacy JSON soubory
v produkčních cestách: kity (kits.json), evaly (evals.json) a mapování rolí
kit→tier (kit_roles.json). DB režim běží na reálném PostgreSQL
(``session_factory`` + ``clean_db``), JSON režim na tmp DATA_DIR — parity
s legacy soubory, dokud Phase F nedokončí kompletní rewire (F10 kontrakt).
"""

import json
from pathlib import Path

from db.repositories.kits import ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from services import evals, kit_catalog, kit_roles

TIER_DEFS = (
    ("HT3", "ladder", "HT3", 3),
    ("HT2", "ladder", "HT2", 2),
    ("S", "ladder", "S", 1),
)


async def _seed_ladder(session_factory, *, kits=(("molepvp", "MolePVP"),)):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, kits, TIER_DEFS)


# ---------------------------------------------------------------------------
# kit_catalog — DB režim
# ---------------------------------------------------------------------------


async def test_kits_empty_table_returns_defaults(session_factory, clean_db, monkeypatch):
    monkeypatch.setattr(kit_catalog, "DEFAULT_KITS", ("FALLBACK",))
    names = await kit_catalog.get_kits(session_factory=session_factory)
    assert names == ["FALLBACK"]


async def test_kits_add_and_list_ordered(session_factory, clean_db):
    await kit_catalog.add_kit("Zebra", session_factory=session_factory)
    await kit_catalog.add_kit("Apple", session_factory=session_factory)
    assert await kit_catalog.get_kits(session_factory=session_factory) == [
        "Apple",
        "Zebra",
    ]


async def test_kits_add_is_case_insensitive_duplicate(session_factory, clean_db):
    assert await kit_catalog.add_kit("MolePVP", session_factory=session_factory)
    assert not await kit_catalog.add_kit(
        "molepvp", session_factory=session_factory
    )
    assert await kit_catalog.get_kits(session_factory=session_factory) == [
        "MolePVP"
    ]


async def test_kits_remove_then_readd_reactivates(session_factory, clean_db):
    await kit_catalog.add_kit("MolePVP", session_factory=session_factory)
    assert await kit_catalog.remove_kit("molepvp", session_factory=session_factory)
    assert await kit_catalog.get_kits(session_factory=session_factory) == []
    assert not await kit_catalog.remove_kit(
        "molepvp", session_factory=session_factory
    )
    assert await kit_catalog.add_kit("MolePVP", session_factory=session_factory)
    assert await kit_catalog.get_kits(session_factory=session_factory) == [
        "MolePVP"
    ]


async def test_kits_canonical_matches_display_case(session_factory, clean_db):
    await _seed_ladder(session_factory)
    assert (
        await kit_catalog.canonical_kit_name("MoLePvP", session_factory=session_factory)
        == "MolePVP"
    )


async def test_kits_canonical_unknown_returns_input(session_factory, clean_db):
    assert (
        await kit_catalog.canonical_kit_name("Unknown", session_factory=session_factory)
        == "Unknown"
    )


# ---------------------------------------------------------------------------
# kit_catalog — JSON režim (parity s legacy kits.json)
# ---------------------------------------------------------------------------


async def test_kits_json_defaults_when_file_missing(tmp_path: Path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert await kit_catalog.get_kits() == list(kit_catalog.DEFAULT_KITS)


async def test_kits_json_add_remove_roundtrip(tmp_path: Path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert await kit_catalog.add_kit("MolePVP")
    seeded = json.loads((tmp_path / "kits.json").read_text())
    assert seeded == list(kit_catalog.DEFAULT_KITS) + ["MolePVP"]
    assert await kit_catalog.remove_kit("molepvp")
    assert json.loads((tmp_path / "kits.json").read_text()) == list(
        kit_catalog.DEFAULT_KITS
    )
    assert not await kit_catalog.remove_kit("molepvp")


# ---------------------------------------------------------------------------
# evals — DB režim
# ---------------------------------------------------------------------------


async def test_evals_unknown_player_or_kit_false(session_factory, clean_db):
    await _seed_ladder(session_factory)
    assert not await evals.has_eval("ghost", "molepvp", session_factory=session_factory)
    assert not await evals.set_eval("ghost", "molepvp", session_factory=session_factory)
    assert not await evals.set_eval("player", "unknown", session_factory=session_factory)


async def test_evals_grant_revoke_lifecycle(session_factory, clean_db):
    await _seed_ladder(session_factory)
    async with transaction(session_factory) as session:
        await PlayerRepository().get_or_create_by_ign(
            session, ign="mendu__", source="discord"
        )
    assert await evals.set_eval("mendu__", "MolePVP", session_factory=session_factory)
    assert await evals.has_eval("mendu__", "molepvp", session_factory=session_factory)
    # unset_eval(True) = revoke
    assert await evals.unset_eval("mendu__", "molepvp", session_factory=session_factory)
    assert not await evals.has_eval("mendu__", "molepvp", session_factory=session_factory)


async def test_evals_double_grant_is_noop_but_ok(session_factory, clean_db):
    await _seed_ladder(session_factory)
    async with transaction(session_factory) as session:
        await PlayerRepository().get_or_create_by_ign(
            session, ign="mendu__", source="discord"
        )
    assert await evals.set_eval("mendu__", "molepvp", session_factory=session_factory)
    assert await evals.set_eval("mendu__", "molepvp", session_factory=session_factory)
    assert await evals.has_eval("mendu__", "molepvp", session_factory=session_factory)


async def test_evals_double_revoke_second_false(session_factory, clean_db):
    await _seed_ladder(session_factory)
    async with transaction(session_factory) as session:
        await PlayerRepository().get_or_create_by_ign(
            session, ign="mendu__", source="discord"
        )
    await evals.set_eval("mendu__", "molepvp", session_factory=session_factory)
    assert await evals.unset_eval("mendu__", "molepvp", session_factory=session_factory)
    assert not await evals.unset_eval("mendu__", "molepvp", session_factory=session_factory)


# ---------------------------------------------------------------------------
# evals — JSON režim (parity s legacy evals.json)
# ---------------------------------------------------------------------------


async def test_evals_json_roundtrip(tmp_path: Path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert not await evals.has_eval("mendu__", "molepvp")
    assert await evals.set_eval("mendu__", "molepvp")
    assert await evals.has_eval("mendu__", "molepvp")
    stored = json.loads((tmp_path / "evals.json").read_text())
    assert set(stored) == {"molepvp"}
    assert set(stored["molepvp"]) == {"mendu__"}
    assert await evals.unset_eval("mendu__", "molepvp")
    assert not await evals.has_eval("mendu__", "molepvp")


# ---------------------------------------------------------------------------
# kit_roles — DB režim
# ---------------------------------------------------------------------------


async def test_kit_role_map_empty_for_unknown_kit(session_factory, clean_db):
    assert await kit_roles.get_kit_role_map("nope", session_factory=session_factory) == {}


async def test_kit_role_set_get_unset(session_factory, clean_db):
    await _seed_ladder(session_factory)
    assert await kit_roles.set_kit_role(
        "molepvp", "HT3", 111, session_factory=session_factory
    )
    assert await kit_roles.set_kit_role(
        "molepvp", "HT2", 222, session_factory=session_factory
    )
    assert await kit_roles.get_kit_role_map(
        "molepvp", session_factory=session_factory
    ) == {"HT3": 111, "HT2": 222}
    assert await kit_roles.unset_kit_role(
        "molepvp", "HT2", session_factory=session_factory
    )
    assert await kit_roles.get_kit_role_map(
        "molepvp", session_factory=session_factory
    ) == {"HT3": 111}


async def test_kit_role_set_replaces_existing_mapping(session_factory, clean_db):
    await _seed_ladder(session_factory)
    await kit_roles.set_kit_role("molepvp", "HT3", 111, session_factory=session_factory)
    assert await kit_roles.set_kit_role(
        "molepvp", "HT3", 999, session_factory=session_factory
    )
    assert await kit_roles.get_kit_role_map(
        "molepvp", session_factory=session_factory
    ) == {"HT3": 999}


async def test_kit_role_unknown_kit_or_tier_false(session_factory, clean_db):
    await _seed_ladder(session_factory, kits=(("other", "Other"),))
    assert not await kit_roles.set_kit_role(
        "unknown", "HT3", 111, session_factory=session_factory
    )
    assert not await kit_roles.set_kit_role(
        "other", "ZZZ", 111, session_factory=session_factory
    )
    assert not await kit_roles.unset_kit_role(
        "other", "HT3", session_factory=session_factory
    )


async def test_kit_role_empty_input_false(session_factory, clean_db):
    await _seed_ladder(session_factory)
    assert not await kit_roles.set_kit_role(
        "", "HT3", 111, session_factory=session_factory
    )
    assert not await kit_roles.set_kit_role(
        "molepvp", "", 111, session_factory=session_factory
    )
    assert not await kit_roles.unset_kit_role(
        "molepvp", "", session_factory=session_factory
    )


async def test_kit_role_all_maps_returns_case_lower_keys(session_factory, clean_db):
    await _seed_ladder(session_factory, kits=(("molepvp", "MolePVP"), ("uke", "UKE")))
    await kit_roles.set_kit_role("molepvp", "S", 333, session_factory=session_factory)
    await kit_roles.set_kit_role("uke", "HT2", 444, session_factory=session_factory)
    all_maps = await kit_roles.get_all_kit_role_maps(
        session_factory=session_factory
    )
    assert all_maps == {"molepvp": {"S": 333}, "uke": {"HT2": 444}}


# ---------------------------------------------------------------------------
# kit_roles — JSON režim (parity s legacy kit_roles.json)
# ---------------------------------------------------------------------------


async def test_kit_role_json_set_get_unset(tmp_path: Path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert await kit_roles.set_kit_role("MolePVP", "HT3", 111)
    assert await kit_roles.get_kit_role_map("molepvp") == {"HT3": "111"}
    stored = json.loads((tmp_path / "kit_roles.json").read_text())
    assert stored == {"molepvp": {"HT3": "111"}}
    assert await kit_roles.unset_kit_role("molepvp", "HT3")
    assert await kit_roles.get_kit_role_map("molepvp") == {}


async def test_kit_role_json_get_unknown_empty(tmp_path: Path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert await kit_roles.get_kit_role_map("nope") == {}


async def test_kit_role_json_unset_missing_false(tmp_path: Path, monkeypatch):
    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert not await kit_roles.unset_kit_role("molepvp", "HT3")
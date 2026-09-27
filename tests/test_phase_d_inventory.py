"""Phase D, D1 — inventory report generator tests (pure, DB-free)."""

from __future__ import annotations

import json

import pytest

from services.phase_d.inventory import inventory_data_dir, render_markdown

PLAYERS = [
    {"username": "AliceMC", "modes": {"IronAxe": "LT3"}, "history": {"IronAxe": [{"date": "16.05.2026", "tier": "LT3"}]}},
    {"username": "alicemc", "modes": {}, "history": {}},
    {"username": "", "modes": {}, "history": {}},
    {"modes": {"GoldSMP": "HT4"}, "history": {"GoldSMP": [{"date": "02.06.2026", "tier": "HT4"}, {"date": "not-a-date", "tier": "LT1"}]}},
    {"username": "solo", "modes": {}, "history": {"MolePVP": [{"date": "01.01.2026", "tier": "NIC JE"}]}},
    "not-a-dict",
]


def _write(data_dir, files):
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        data_dir.joinpath(name).write_text(
            json.dumps(files[name], ensure_ascii=False), encoding="utf-8"
        )


def test_inventory_players_counts_and_issues(tmp_path):
    data = tmp_path / "data"
    _write(data, {"players.json": PLAYERS})

    report = inventory_data_dir(data)
    players = report["players"]

    assert players["total"] == 6
    assert players["non_dict_records"] == [5]
    assert players["no_username"] == ["3"]
    assert players["empty_ign"] == ["2"]
    assert players["duplicate_igns"] == {"alicemc": [0, 1]}
    assert len(players["invalid_dates"]) == 1
    assert players["invalid_dates"][0]["date"] == "not-a-date"
    assert "NIC JE" in players["tier_codes"]
    assert "MolePVP" in players["kits"]
    assert players["history_entries"] == 4


def test_inventory_cooldowns_and_ht3(tmp_path):
    data = tmp_path / "data"
    _write(
        data,
        {
            "cooldowns.json": {"111": 1780593929525},
            "ht3_cooldowns.json": {"111": {"AnchorPvP": 1785769543952, "IronAxe": 1}},
        },
    )
    report = inventory_data_dir(data)
    assert report["cooldowns_waitlist"]["records"] == 1
    assert report["ht3_cooldowns"]["players"] == 1
    assert report["ht3_cooldowns"]["kit_counts"] == {"AnchorPvP": 1, "IronAxe": 1}
    assert "unresolved" in report["cooldowns_waitlist"]["all_unresolved_reason"]


def test_inventory_kits_testers_stats_and_not_imported(tmp_path):
    data = tmp_path / "data"
    _write(
        data,
        {
            "kits.json": ["AnchorPvP", "IronAxe"],
            "testers.json": ["111", "222"],
            "testers_stats.json": {"111": {"total": 1}},
        },
    )
    report = inventory_data_dir(data)
    assert report["kits_file"]["kits"] == ["AnchorPvP", "IronAxe"]
    assert report["testers"]["ids"] == ["111", "222"]
    assert report["testers_stats"]["players"] == 1
    assert "testers_stats.json" in report["not_imported_to_relational"]


def test_inventory_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        inventory_data_dir(tmp_path / "nope")


def test_inventory_empty_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    report = inventory_data_dir(data)
    assert report["files_present"] == []
    assert report["players"] is None


def test_render_markdown_contains_sections(tmp_path):
    data = tmp_path / "data"
    _write(data, {"players.json": PLAYERS[:1]})
    md = render_markdown(inventory_data_dir(data))
    assert "## players.json" in md
    assert "total: 1" in md
    assert "Do relačního schématu se NEimportuje" in md
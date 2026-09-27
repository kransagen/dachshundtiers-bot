"""Phase E, E8 — JSON compatibility zero-path contract tests."""

from __future__ import annotations

from pathlib import Path

from services.phase_e.json_compat import (
    FORBIDDEN_FLOWS,
    SAFE_TIER_READER_FLOWS,
    build_json_compat_report,
    render_markdown_report,
    scan_io,
)

REPO = Path(__file__).resolve().parents[1]


def test_report_inventory_finds_readers_and_writers():
    report = build_json_compat_report(REPO)
    assert report["readers"]
    assert report["writers"]
    assert all(s["module"] and s["file"] and s["line_text"] for s in report["sites"])


def test_players_json_is_the_only_current_tier_artifact():
    report = build_json_compat_report(REPO)
    tier_files = {s["file"] for s in report["tier_readers"]} | {
        s["file"] for s in report["tier_writers"]
    }
    assert tier_files == {"players.json"}


def test_zero_json_to_discord_path():
    report = build_json_compat_report(REPO)
    assert report["json_to_discord_violations"] == []
    assert report["zero_json_to_discord"] is True


def test_zero_json_to_authoritative_pg_current_tier_path():
    report = build_json_compat_report(REPO)
    assert report["json_to_pg_violations"] == []
    assert report["zero_json_to_pg"] is True


def test_report_conclusion_and_markdown():
    report = build_json_compat_report(REPO)
    assert report["conclusion"] == "no JSON path can determine Discord current tier"
    md = render_markdown_report(report)
    assert "JSON current tier → Discord: 0" in md
    assert "JSON current tier → authoritative PG current tier: 0" in md


def test_scan_io_detects_synthetic_tier_reader(tmp_path):
    (tmp_path / "cogs").mkdir()
    (tmp_path / "cogs" / "evil.py").write_text(
        "from storage import load_data\n"
        "load_data('players.json', [])\n"
        "member.edit(roles=[])\n",
        encoding="utf-8",
    )
    sites = scan_io(tmp_path)
    assert any(s["file"] == "players.json" for s in sites)
    report = build_json_compat_report(tmp_path)
    assert report["tier_readers"]
    assert all(s["flow"] == "unclassified" for s in report["tier_readers"])
    assert report["json_to_pg_violations"] != []
    assert report["zero_json_to_pg"] is False


def test_every_repo_tier_reader_is_classified():
    report = build_json_compat_report(REPO)
    for site in report["tier_readers"]:
        assert site["flow"] not in {"unclassified", *FORBIDDEN_FLOWS}
        assert site["flow"] in SAFE_TIER_READER_FLOWS.values()


def test_every_repo_tier_writer_is_classified():
    """H5 extension: writers must be explicitly reviewed too, not just readers."""
    report = build_json_compat_report(REPO)
    for site in report["tier_writers"]:
        assert site["flow"] not in {"unclassified", *FORBIDDEN_FLOWS}
        assert site["flow"] in SAFE_TIER_READER_FLOWS.values()


def test_transaction_get_set_are_detected_as_json_io(tmp_path):
    """H5 extension: Transaction.get/set (services/store.py) — the actual
    dominant production read/write path for players.json — must be detected,
    not just the legacy load_data/save_data names."""
    (tmp_path / "cogs").mkdir()
    (tmp_path / "cogs" / "evil.py").write_text(
        "async def _run(tx):\n"
        "    players = tx.get('players.json', [])\n"
        "    tx.set('players.json', players)\n",
        encoding="utf-8",
    )
    sites = scan_io(tmp_path)
    funcs = {s["func"] for s in sites}
    assert "tx.get" in funcs
    assert "tx.set" in funcs
    report = build_json_compat_report(tmp_path)
    assert report["tier_readers"] and report["tier_writers"]
    assert all(s["flow"] == "unclassified" for s in report["tier_readers"])
    assert all(s["flow"] == "unclassified" for s in report["tier_writers"])
    assert report["json_to_pg_violations"] != []


def test_json_compat_survives_unrelated_line_number_drift(tmp_path):
    """H5 regression: shifting lines above a reviewed players.json call site
    must not turn it into a false 'unclassified' (== json_to_pg violation)."""
    import shutil

    workdir = tmp_path / "dachshundtiers-bot"
    shutil.copytree(
        REPO,
        workdir,
        ignore=shutil.ignore_patterns(
            ".venv", ".venv_audit", "__pycache__", ".git", "backups",
            ".ruff_cache", ".pytest_cache",
        ),
    )
    target = workdir / "services" / "tickets.py"
    original = target.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    mutated = lines[:5] + ["\n"] * 5 + lines[5:]
    target.write_text("".join(mutated), encoding="utf-8")

    report = build_json_compat_report(workdir)
    assert report["json_to_pg_violations"] == [], (
        "line-number drift alone must never turn a reviewed site unclassified: "
        f"{report['json_to_pg_violations']}"
    )


def test_scan_io_ignores_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "fake.py").write_text(
        "load_data('players.json')\n", encoding="utf-8"
    )
    assert scan_io(tmp_path) == []
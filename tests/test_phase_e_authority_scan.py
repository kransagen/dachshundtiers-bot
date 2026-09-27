"""Phase E, E13 — authority direction contract tests."""

from __future__ import annotations

from pathlib import Path

from services.phase_e.authority_scan import (
    AUTHORIZED_MUTATION_SITES,
    DISCORD_MUTATION_PATTERNS,
    build_authority_report,
    discord_mutation_violations,
    render_markdown_report,
    scan_patterns,
)

REPO = Path(__file__).resolve().parents[1]


def test_zero_discord_mutations_in_db_and_services():
    sites = scan_patterns(REPO, DISCORD_MUTATION_PATTERNS)
    non_cog = [s for s in sites if not s["module"].startswith("cogs.")]
    assert non_cog == []


def test_mutation_surface_matches_authorized_sites():
    sites = scan_patterns(REPO, DISCORD_MUTATION_PATTERNS)
    found = {(s["module"], s["qualname"], s["pattern"]) for s in sites}
    assert found == AUTHORIZED_MUTATION_SITES


def test_mutation_surface_survives_unrelated_line_number_drift(tmp_path):
    """H5 regression: inserting unrelated blank lines above an authorized call
    site must NOT make the allow-list flag it as a violation — the identity is
    the enclosing function's qualname, not the line number."""
    import shutil

    # _module_of() derives the dotted module path by locating a
    # "dachshundtiers-bot" path segment, so the copy must keep that name.
    workdir = tmp_path / "dachshundtiers-bot"
    shutil.copytree(
        REPO,
        workdir,
        ignore=shutil.ignore_patterns(
            ".venv", ".venv_audit", "__pycache__", ".git", "backups",
            ".ruff_cache", ".pytest_cache",
        ),
    )
    target = workdir / "cogs" / "roles.py"
    original = target.read_text(encoding="utf-8")
    # Insert 5 unrelated blank lines near the top of the file, shifting every
    # subsequent line number without touching the authorized call itself.
    lines = original.splitlines(keepends=True)
    mutated = lines[:5] + ["\n"] * 5 + lines[5:]
    target.write_text("".join(mutated), encoding="utf-8")

    sites = scan_patterns(workdir, DISCORD_MUTATION_PATTERNS)
    violations = discord_mutation_violations(sites)
    assert violations == [], (
        "line-number drift alone must never produce a new authority violation: "
        f"{violations}"
    )


def test_pg_current_tier_only_in_db():
    report = build_authority_report(REPO)
    assert report["pg_tier_violations"] == []
    assert all(s["module"].startswith("db.") for s in report["pg_tier_sites"])


def test_github_export_only():
    report = build_authority_report(REPO)
    assert report["github_export_only"] is True


def test_no_json_to_discord_or_pg():
    report = build_authority_report(REPO)
    assert report["json_to_discord"] == []
    assert report["json_to_pg"] == []


def test_conclusion_and_markdown():
    report = build_authority_report(REPO)
    assert report["conclusion"].startswith(
        "authority directions hold: no DB->Discord, no GitHub->DB"
    )
    md = render_markdown_report(report)
    assert "discord mutation violations: 0" in md


def test_synthetic_unauthorized_mutation_is_flagged(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "evil.py").write_text(
        "async def f():\n    member.edit(roles=[])\n",
        encoding="utf-8",
    )
    sites = scan_patterns(tmp_path, DISCORD_MUTATION_PATTERNS)
    assert discord_mutation_violations(sites) != []


def test_synthetic_pg_tier_in_export_module_is_flagged(tmp_path):
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "evil.py").write_text(
        "player_current_tiers = []\n",
        encoding="utf-8",
    )
    report = build_authority_report(tmp_path)
    assert report["pg_tier_violations"] != []
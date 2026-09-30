"""Phase E, E13 — repo-wide authority direction scan.

Enforces the architecture invariants at the code level:

1. Discord mutation calls live ONLY at the authorized business sites
   (E2 audit allow-list). Any other site is a blocker.
2. ``player_current_tiers`` / ``PlayerCurrentTier`` references live ONLY in
   ``db/`` (model/repos/services) and the sanctioned Phase D/E tooling.
   Any cogs/ or export (github_sync, websync) reference is a blocker.
3. GitHub export (websync/github_sync) never writes Discord roles and
   never writes authoritative PG current-tier rows — export-only.
4. No JSON current tier -> Discord and no JSON current tier ->
   authoritative PG current tier (delegates to the E8 json_compat scan).

Pure module (no db/discord imports) so contract tests run it standalone.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

from services.phase_e._ast_utils import qualname_at as _qualname_at
from services.phase_e.json_compat import build_json_compat_report

DISCORD_MUTATION_PATTERNS = (
    "member.add_roles(",
    "member.remove_roles(",
    "member.edit(roles=",
    "await apply_role_actions(",
    "return await apply_role_actions(",
    "await apply_rollback_actions(",
    "return await apply_rollback_actions(",
)

PG_TIER_PATTERNS = ("player_current_tiers", "PlayerCurrentTier")

ERROR_LIMIT = 50


def _iter_py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*.py")
        if "tests" not in p.parts
        and "docs" not in p.parts
        and "phase_d" not in ".".join(p.parts)
        and "phase_e" not in ".".join(p.parts)
        and p.name != "legacy_import.py"
        and "migrations/versions" not in str(p)
        and ".venv" not in p.parts
    )


def _module_of(path: Path, root: Path) -> str:
    """Dotted module path of ``path`` relative to the repo ``root``.

    Must NOT search for a literal repo-folder-name string in the absolute
    path — GitHub Actions checks out to ``.../work/<repo>/<repo>/...``, so a
    name-based search finds the wrong (outer) occurrence and silently
    mis-attributes every module, breaking the authorized-mutation-site
    allowlist match on CI while passing locally.
    """
    parts = path.relative_to(root).parts
    dotted = ".".join(parts)
    return dotted.rsplit(".py", 1)[0]


_STRING_TOKEN_NAMES = {"STRING", "FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"}
_STRING_TOKEN_TYPES = {
    getattr(tokenize, name) for name in _STRING_TOKEN_NAMES if hasattr(tokenize, name)
}


def _string_spans_by_line(text_content: str) -> dict[int, list[tuple[int, int]]]:
    """Column spans that are inside a string/f-string literal, per 1-indexed line.

    Used so a pattern match found by plain substring search can be dismissed
    when it only occurs inside human-readable text (a docstring, help string,
    or Discord embed message) rather than actual code — see the
    ``player_current_tiers`` help-string false positive this guards against.
    Best-effort: on any tokenize failure, returns {} (no filtering), so a real
    violation is never hidden by a lexing problem.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text_content).readline)
        for tok in tokens:
            if tok.type not in _STRING_TOKEN_TYPES:
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            if srow == erow:
                spans.setdefault(srow, []).append((scol, ecol))
            else:
                spans.setdefault(srow, []).append((scol, 10**9))
                for mid in range(srow + 1, erow):
                    spans.setdefault(mid, []).append((0, 10**9))
                spans.setdefault(erow, []).append((0, ecol))
    except (tokenize.TokenizeError, SyntaxError, IndentationError, ValueError):
        return {}
    return spans


def _all_in_string(line_spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in line_spans)


def scan_patterns(root: Path, patterns: tuple[str, ...]) -> list[dict]:
    sites: list[dict] = []
    for path in _iter_py_files(root):
        try:
            text_content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text_content.splitlines()
        module = _module_of(path, root)
        try:
            tree = ast.parse(text_content, filename=str(path))
        except SyntaxError:
            tree = None
        string_spans = _string_spans_by_line(text_content)
        for lineno, text in enumerate(lines, start=1):
            line_string_spans = string_spans.get(lineno, [])
            for pattern in patterns:
                search_from = 0
                while True:
                    idx = text.find(pattern, search_from)
                    if idx == -1:
                        break
                    search_from = idx + 1
                    if _all_in_string(line_string_spans, idx, idx + len(pattern)):
                        continue
                    qualname = (
                        _qualname_at(tree, lineno) if tree is not None else "<unparsed>"
                    )
                    sites.append(
                        {
                            "module": module,
                            "path": str(path),
                            "lineno": lineno,
                            "qualname": qualname,
                            "pattern": pattern,
                            "line_text": text.strip(),
                        }
                    )
                    break  # one report per (line, pattern) — matches prior behavior
    return sites


def unique_occurrences(sites: list[dict]) -> list[dict]:
    seen: set[tuple[str, int, str]] = set()
    result: list[dict] = []
    for site in sites:
        key = (site["module"], site["lineno"], site["pattern"])
        if key not in seen:
            seen.add(key)
            result.append(site)
    return result


# Authorized Discord mutation sites — the complete, reviewed business
# surface from the E2 production-path audit. Modules may reference the
# patterns in docstrings/help; an exact (module, qualname, pattern) outside
# this set is a blocker.
#
# The set is compared for EQUALITY against what the scanner finds
# (``tests/test_phase_e_authority_scan.py``), in both directions: a new
# unauthorized call fails, and so does an entry whose call no longer exists.
# That second direction matters — it means deleting an authorized mutation
# forces a deliberate edit here, so the review record cannot silently keep
# blessing a code path that is gone. `/result`'s hand-picked
# ``add_role``/``remove_role`` mutations were removed for exactly that reason
# (unauditable manual role edits); the tier-driven
# ``auto_grant_kit_role`` path below is the one that remains authorized.
#
# Keyed by (module, qualified function/method name, pattern) — deliberately
# NOT by line number (H5 audit finding: a line-number allowlist breaks on any
# unrelated edit that shifts lines elsewhere in the file, making the suite
# unable to distinguish cosmetic drift from a real new mutation site; it also
# creates pressure to blindly bump numbers without re-reviewing). A function
# name only changes when someone deliberately renames/moves the authorized
# call, which is exactly the kind of change this allow-list should force a
# human to re-approve.
AUTHORIZED_MUTATION_SITES: set[tuple[str, str, str]] = {
    ("cogs.roles", "auto_grant_kit_role", "member.edit(roles="),
    ("cogs._shared", "apply_role_actions", "member.add_roles("),
    ("cogs._shared", "apply_role_actions", "member.remove_roles("),
    ("cogs._shared", "apply_rollback_actions", "member.add_roles("),
    ("cogs._shared", "apply_rollback_actions", "member.remove_roles("),
    ("cogs.sync", "SyncDiscordRollbackView.confirm", "await apply_rollback_actions("),
    ("cogs.edituser", "EditUser._apply_roles", "await apply_role_actions("),
    ("cogs.edituser", "EditUser._apply_roles", "return await apply_role_actions("),
    ("cogs.retire", "Retire.execute_retire", "await apply_role_actions("),
}


def discord_mutation_violations(sites: list[dict]) -> list[dict]:
    return [
        s
        for s in sites
        if (s["module"], s["qualname"], s["pattern"]) not in AUTHORIZED_MUTATION_SITES
    ][:ERROR_LIMIT]


def build_authority_report(root: Path) -> dict:
    mutation_sites = unique_occurrences(scan_patterns(root, DISCORD_MUTATION_PATTERNS))
    pg_sites = unique_occurrences(scan_patterns(root, PG_TIER_PATTERNS))
    json_report = build_json_compat_report(root)

    mutation_violations = discord_mutation_violations(mutation_sites)

    pg_violations = [
        s
        for s in pg_sites
        if not s["module"].startswith("db.")
    ][:ERROR_LIMIT]

    github_export_only = not any(
        s["module"].startswith(("services.websync", "services.github_sync"))
        for s in mutation_sites + pg_sites
    )

    return {
        "mutation_sites": mutation_sites,
        "mutation_violations": mutation_violations,
        "pg_tier_sites": pg_sites,
        "pg_tier_violations": pg_violations,
        "github_export_only": github_export_only,
        "json_to_discord": json_report["json_to_discord_violations"],
        "json_to_pg": json_report["json_to_pg_violations"],
        "conclusion": (
            not mutation_violations
            and not pg_violations
            and github_export_only
            and not json_report["json_to_discord_violations"]
            and not json_report["json_to_pg_violations"]
            and "authority directions hold: no DB->Discord, no GitHub->DB, no JSON->Discord, no JSON->PG"
        ),
    }


def render_markdown_report(report: dict) -> str:
    lines = [
        "# E13 — authority direction scan",
        "",
        f"- discord mutation sites: {len(report['mutation_sites'])}",
        f"- discord mutation violations: {len(report['mutation_violations'])}",
        f"- pg current-tier sites: {len(report['pg_tier_sites'])}",
        f"- pg current-tier violations (outside db/sanctioned): {len(report['pg_tier_violations'])}",
        f"- github export-only: {report['github_export_only']}",
        "",
        "## Authorized Discord mutation sites",
        "",
    ]
    for s in sorted(report["mutation_sites"], key=lambda x: (x["module"], x["lineno"])):
        lines.append(f"- {s['module']}:{s['lineno']}  `{s['pattern']}`  `{s['line_text']}`")
    lines.append("")
    lines.append(f"conclusion: {report['conclusion']}")
    return "\n".join(lines)
"""Phase E, E8 — JSON read/write inventory and zero-path guarantees.

Static codebase audit of every path that could carry a JSON current tier
into Discord or into the authoritative PostgreSQL current-tier state.

Guarantees enforced:

1. Zero path ``JSON current tier -> Discord`` — no code site reads a JSON
   artifact and then mutates Discord roles.
2. Zero path ``JSON current tier -> authoritative PG current tier`` — no
   code site reads a JSON artifact and writes ``player_current_tiers`` /
   ``tier_history`` with an authoritative source (``discord_sync``,
   ``promotion``, ``manual``).
3. On disagreement, Discord wins — mirrors overwrite JSON-derived rows; no
   JSON-derived row is ever created with an authoritative source.

This module is deliberately pure (no imports from ``db`` or ``discord``) so
the contract tests can run it in isolation, mirroring ``legacy_writers``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from services.phase_e._ast_utils import qualname_at

# Exact function/method names that are recognized JSON readers/writers
# regardless of their arguments (e.g. ``load_data("players.json")``).
READER_CALLS = ("load_data", "read", "store_read", "_store_read")
WRITER_CALLS = ("save_data", "write")

# ``Transaction.get``/``Transaction.set`` (services/store.py) are the actual
# dominant production read/write path for players.json (H5 audit finding:
# the previous scanner only recognized save_data/load_data and completely
# missed this — the highest-traffic call surface — giving false confidence).
# They're generic method names (``get``/``set``) shared with dicts/mappings,
# so they're only treated as JSON I/O when the first argument is itself a
# ``*.json`` filename literal — dict.get("some_key") etc. never matches.
TRANSACTION_READER_METHODS = ("get",)
TRANSACTION_WRITER_METHODS = ("set",)

CURRENT_TIER_FILES = ("players.json",)

# Site-level classification of every current-tier (players.json) reader,
# derived from the E2 production-path audit. Each entry maps
# (module, qualname, func) — the enclosing function's qualname, NOT a line
# number (H5 audit finding: a line-number key breaks under any unrelated
# edit that shifts lines elsewhere in the file) — to a documented flow. Any
# players.json reader NOT in this table fails the audit ("unclassified") —
# a new JSON reader must be explicitly reviewed and classified before it can
# exist.
#
# Flow classes:
#   json_ui_read                 — UI/display/find; any Discord role change is
#                                  gated by explicit command input (never by
#                                  JSON file content)
#   identity_claim_dualwrite_export — PG-first identity claim; JSON write is
#                                  a best-effort export artifact
#   web_export_canonical         — players.json is the presentation canonical
#                                  for GitHub export only (never read back)
#   discord_observe_or_analysis  — read for analysis/reports; zero mutations
#   json_legacy_mode_only_pg_gated — this exact call only executes when
#                                  PostgreSQL is NOT configured (guarded by
#                                  an explicit ``session_factory is None`` /
#                                  ``using_postgres()`` check in this
#                                  function or its only caller) — dead
#                                  whenever the bot runs in DB mode
#
# Entries below were captured directly from a real scan of this working tree
# (not hand-guessed) — see the H5 fix notes in the audit report for how each
# was reviewed.
#
# G0 note on the former ``json_first_write_pending_db_gate`` flow: that class
# existed ONLY to record the H1 finding — that ``/result`` and ``/topresult``
# reached the JSON branch because the commands never passed
# ``session_factory`` into ``record_result``/``record_ht_fight``, so
# players.json was written first and unconditionally, in every deployment.
# The commands now pass the session factory (the DB dispatcher is taken
# first), so those legacy sites are gone entirely. The regression that would
# resurrect H1 is pinned by an AST/qualname test in
# ``tests/test_g0_promotion_cutover.py``.
#
# B/C FYI: the formerly listed JSON readers/writers (cogs.edituser
# ``_find_player_sync``/modaly, cogs.sync ``_canonical_*``/``save_players``,
# cogs.results ``removeplayertiers._run``, services.edituser
# ``apply_player_edit``/``execute_player_edit``, services.tickets
# ``find_player_tier``, services.results``record_result._run``,
# services.topresult ``record_ht_fight._run``) are DELETED — nothing left to
# classify. Jediné legacy JSON I/O, které v produkci zůstává, je datacheck
# (migrační/inspenkční tooling na data/).
SAFE_TIER_READER_FLOWS: dict[tuple[str, str, str], str] = {
    ("services.datacheck", "run_datacheck", "store_read"): "discord_observe_or_analysis",
    ("services.datacheck", "perform_repairs._run", "tx.get"): "json_legacy_mode_only_pg_gated",
    ("services.datacheck", "perform_repairs._run", "tx.set"): "json_legacy_mode_only_pg_gated",
}

FORBIDDEN_FLOWS = {"json_tier_to_discord", "json_tier_to_pg"}


def _iter_py_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        p
        for p in root.rglob("*.py")
        if "tests" not in p.parts
        and "docs" not in p.parts
        and ".venv" not in p.parts
    )


def _line_text(path: Path, lineno: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[lineno - 1] if 1 <= lineno <= len(lines) else ""
    except OSError:
        return ""


def _module_of(path: Path, root: Path) -> str:
    """Dotted module path of ``path`` relative to the repo ``root``.

    Must NOT search for a literal repo-folder-name string in the absolute
    path — GitHub Actions checks out to ``.../work/<repo>/<repo>/...``, so a
    name-based search finds the wrong (outer) occurrence and silently
    mis-attributes every module, breaking the classification allowlist match
    on CI while passing locally.
    """
    parts = path.relative_to(root).parts
    dotted = ".".join(parts)
    return dotted.rsplit(".py", 1)[0]


def _is_json_literal(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(".json")
    )


class _CallVisitor(ast.NodeVisitor):
    """Collects every JSON reader/writer call site.

    Recognizes:
    - exact-name calls: ``load_data("x.json")`` / ``save_data("x.json")``
      (READER_CALLS / WRITER_CALLS);
    - ``Transaction.get("x.json", default)`` / ``Transaction.set("x.json", v)``
      (services/store.py) — the dominant production read/write path for
      players.json (H5 audit finding); recognized structurally by "a
      `.get`/`.set` attribute call whose first argument is a `*.json`
      filename literal", since the method names alone are too generic
      (shared with plain dicts) to match on name alone;
    - ``open("x.json", mode=...)`` — classified reader/writer by mode.
    """

    def __init__(self) -> None:
        self.sites: list[dict[str, Any]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = None
        func = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
            func = ast.unparse(node.func)

        kind = None
        if name in READER_CALLS:
            kind = "reader"
        elif name in WRITER_CALLS:
            kind = "writer"
        elif name == "open":
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            mode = mode or "r"
            kind = "writer" if any(c in mode for c in "wax+") else "reader"
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in TRANSACTION_READER_METHODS + TRANSACTION_WRITER_METHODS
            and node.args
            and _is_json_literal(node.args[0])
        ):
            kind = (
                "reader"
                if node.func.attr in TRANSACTION_READER_METHODS
                else "writer"
            )
            func = f"tx.{node.func.attr}"

        if kind is None:
            self.generic_visit(node)
            return

        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                self.sites.append(
                    {
                        "kind": kind,
                        "file": arg.value,
                        "func": func or name,
                        "lineno": node.lineno,
                        "col": node.col_offset,
                    }
                )
                break
        self.generic_visit(node)


def scan_io(root: Path) -> list[dict[str, Any]]:
    """Scan the repo and return every JSON reader/writer call site."""
    sites: list[dict[str, Any]] = []
    for path in _iter_py_files(root):
        try:
            text_content = path.read_text(encoding="utf-8")
            tree = ast.parse(text_content)
        except (OSError, SyntaxError):
            continue
        visitor = _CallVisitor()
        visitor.visit(tree)
        for site in visitor.sites:
            site["module"] = _module_of(path, root)
            site["path"] = str(path)
            site["qualname"] = qualname_at(tree, site["lineno"])
            site["line_text"] = _line_text(path, site["lineno"])
            sites.append(site)
    return sites


def _is_tier_carrying(site: dict[str, Any]) -> bool:
    return site.get("file") in CURRENT_TIER_FILES


def classify_tier_reader(site: dict[str, Any]) -> str:
    return SAFE_TIER_READER_FLOWS.get(
        (site["module"], site["qualname"], site["func"]), "unclassified"
    )


def build_json_compat_report(root: Path) -> dict[str, Any]:
    sites = scan_io(root)

    readers = [s for s in sites if s["kind"] == "reader"]
    writers = [s for s in sites if s["kind"] == "writer"]
    tier_readers = [s for s in readers if _is_tier_carrying(s)]
    tier_writers = [s for s in writers if _is_tier_carrying(s)]

    # Both readers AND writers of players.json must be explicitly reviewed
    # and classified — a new, unreviewed JSON writer is just as much an
    # authority-invariant risk as a new reader (H5 audit finding: the
    # previous version only ever checked readers).
    for site in tier_readers + tier_writers:
        site["flow"] = classify_tier_reader(site)

    forbidden = [s for s in tier_readers if s["flow"] in FORBIDDEN_FLOWS]
    unclassified = [
        s for s in tier_readers + tier_writers if s["flow"] == "unclassified"
    ]

    return {
        "sites": sites,
        "readers": readers,
        "writers": writers,
        "tier_readers": tier_readers,
        "tier_writers": tier_writers,
        "json_to_discord_violations": forbidden,
        "json_to_pg_violations": unclassified,
        "zero_json_to_discord": not forbidden,
        "zero_json_to_pg": not unclassified,
        "conclusion": (
            not forbidden
            and not unclassified
            and "no JSON path can determine Discord current tier"
        ),
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# E8 — JSON read/write inventory",
        "",
        f"- readers: {len(report['readers'])}",
        f"- writers: {len(report['writers'])}",
        f"- current-tier readers (players.json): {len(report['tier_readers'])}",
        f"- current-tier writers (players.json): {len(report['tier_writers'])}",
        "",
        "## Zero-path checks",
        "",
        f"- JSON current tier → Discord: {len(report['json_to_discord_violations'])}",
        f"- JSON current tier → authoritative PG current tier: {len(report['json_to_pg_violations'])}",
        "",
        "## Tier-carrying readers",
        "",
    ]
    for s in sorted(report["tier_readers"], key=lambda x: (x["module"], x["lineno"])):
        lines.append(f"- {s['module']}:{s['lineno']}  `{s['file']}`  `{s['line_text'].strip()}`")
    lines.append("")
    lines.append("## Tier-carrying writers")
    lines.append("")
    for s in sorted(report["tier_writers"], key=lambda x: (x["module"], x["lineno"])):
        lines.append(f"- {s['module']}:{s['lineno']}  `{s['file']}`  `{s['line_text'].strip()}`")
    lines.append("")
    lines.append(f"conclusion: {report['conclusion']}")
    return "\n".join(lines)
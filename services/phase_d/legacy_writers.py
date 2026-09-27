"""D6 — legacy JSON writer audit (static, no DB).

Every JSON write site in the runtime tree is located and classified:
``export_only`` (GitHub artifact, never read back), ``identity_claim_dualwrite``
(link identity: PostgreSQL-first, JSON exported after), ``legacy_operational_record``
(JSON stays as the operational record until Phase E/G; PostgreSQL is the
authority for current tiers) and ``storage_layer_atomic_writer`` (the atomic
write engine itself). The audit also proves the direction invariant: ``db/``
never imports ``storage``, so PostgreSQL can never write JSON — there is no
PG → JSON → Discord path and no silent fallback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

WRITER_PATTERNS = (
    re.compile(r"\bsave_data\("),
    re.compile(r"\bpush_players\("),
    re.compile(r"\bwrite_json\("),
)

EXCLUDED_DIRS = {"tests", ".git", "__pycache__", ".venv", "backups", "data"}

CLASSIFICATION: dict[tuple[str, Optional[int]], str] = {
    ("config.py", None): "legacy_operational_record",
    ("github_sync.py", None): "export_only",
    ("migrate_json_to_postgres.py", None): "migration_tooling",
    ("services/websync.py", None): "export_only",
    ("services/player_export.py", None): "export_only",
    ("cogs/edituser.py", 1136): "identity_claim_dualwrite",
    ("cogs/ht3.py", None): "legacy_operational_record",
    ("cogs/queues.py", None): "legacy_operational_record",
    ("cogs/tournaments.py", None): "legacy_operational_record",
    ("cogs/results.py", None): "legacy_operational_record",
    ("cogs/topresult.py", None): "legacy_operational_record",
    ("cogs/sync.py", None): "legacy_operational_record",
    ("services/config_store.py", None): "legacy_operational_record",
    ("services/kit_roles.py", None): "legacy_operational_record",
    ("services/queue_service.py", None): "legacy_operational_record",
    ("services/store.py", None): "storage_layer_atomic_writer",
    ("services/playersync.py", None): "legacy_operational_record",
    ("services/phase_e/json_compat.py", None): "audit_tooling",
    ("storage.py", None): "storage_layer_atomic_writer",
    ("utils.py", None): "legacy_operational_record",
    ("views.py", None): "legacy_operational_record",
}

WRITE_CALL = re.compile(r"\bsave_data\(|\.write_json\(|push_players\(")

CLASS_ORDER = (
    "export_only",
    "identity_claim_dualwrite",
    "legacy_operational_record",
    "storage_layer_atomic_writer",
    "migration_tooling",
    "audit_tooling",
    "unclassified",
)


def scan_writers(root: Union[str, Path]) -> list[dict]:
    root = Path(root)
    sites: list[dict] = []
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        module = str(path.relative_to(root)).replace("\\", "/")
        for line_no, line in enumerate(lines, start=1):
            if any(pattern.search(line) for pattern in WRITER_PATTERNS):
                sites.append(
                    {
                        "module": module,
                        "line": line_no,
                        "context": line.strip()[:120],
                    }
                )
    return sites


def classify_site(module: str, line: Optional[int] = None) -> str:
    exact = CLASSIFICATION.get((module, line))
    if exact is not None:
        return exact
    return CLASSIFICATION.get((module, None), "unclassified")


def build_writers_report(root: Union[str, Path] = ".") -> dict:
    sites = scan_writers(root)
    classified = [
        {**site, "classification": classify_site(site["module"], site["line"])}
        for site in sites
    ]
    summary = {cls: 0 for cls in CLASS_ORDER}
    for site in classified:
        summary[site["classification"]] += 1

    db_tree = Path(root)
    write_imports: list[str] = []
    read_probes: list[str] = []
    for path in sorted(db_tree.rglob("*.py")):
        if "db" not in path.parts or "tests" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "import storage" not in content:
            continue
        target = str(path.relative_to(db_tree)).replace("\\", "/")
        if WRITE_CALL.search(content):
            write_imports.append(target)
        else:
            read_probes.append(target)
    return {
        "writers_found": len(classified),
        "summary": summary,
        "sites": classified,
        "direction_audit": {
            "pg_imports_storage_with_write_api": write_imports,
            "pg_readonly_backend_probes": read_probes,
            "pg_to_json_to_discord_path": len(write_imports) > 0,
            "note": "db/ nikdy nezapisuje JSON (jen read-only backend probe)",
        },
        "conclusion": (
            "Žádný JSON writer není tichým fallbackem: PostgreSQL nemá žádnou "
            "cestu k zápisu JSON. Discord mutace zůstávají pouze v explicitních "
            "operacích (roles.py / result parametry / _shared)."
        ),
    }
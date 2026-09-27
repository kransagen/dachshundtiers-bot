"""D3 — unresolved-identity report (read-only, no guessing).

Answers: which players are still unlinked (IGN-only records), and which
discord-keyed legacy records remain unresolved after D2's import. The ONLY
resolution path is an explicit Discord ID link (``/linkdiscord``); this report
never proposes identities, merges, or fuzzy matches.

PostgreSQL-only. Reads ``players`` and open ``migration_import_issues``.
"""

from __future__ import annotations

from sqlalchemy import func, select

from db.models import MigrationImportIssue, Player

REPORT_RECOMMENDATION = (
    "Nepřiřazené záznamy se řeší JEN explicitním propojením Discord ID "
    "(/linkdiscord). Žádné automatické párování ani hádání identity se "
    "neprovádí — toto je záměr (authority model, design §16)."
)

SAMPLE_LIMIT = 3
UNLINKED_LIMIT = 500


def _anonymize(payload: dict) -> dict:
    """Keys useful for triage, values limited; payloads may hold Discord IDs."""
    return {k: str(v)[:200] for k, v in list(payload.items())[:6]}


async def build_unresolved_report(session_factory) -> dict:
    async with session_factory() as session:
        players_total = (
            await session.execute(select(func.count()).select_from(Player))
        ).scalar_one()
        players_linked = (
            await session.execute(
                select(func.count())
                .select_from(Player)
                .where(Player.discord_id.is_not(None))
            )
        ).scalar_one()
        unlinked_rows = (
            (
                await session.execute(
                    select(Player.id, Player.ign)
                    .where(Player.discord_id.is_(None))
                    .order_by(Player.ign)
                    .limit(UNLINKED_LIMIT)
                )
            )
            .all()
        )

        issue_rows = (
            (
                await session.execute(
                    select(
                        MigrationImportIssue.category,
                        MigrationImportIssue.source_file,
                        MigrationImportIssue.source_key,
                        MigrationImportIssue.reason,
                        MigrationImportIssue.payload,
                        MigrationImportIssue.id,
                    ).where(MigrationImportIssue.status == "open")
                )
            )
            .all()
        )

    by_category: dict[str, dict] = {}
    for category, source_file, source_key, reason, payload, issue_id in issue_rows:
        bucket = by_category.setdefault(
            category, {"count": 0, "source_files": set(), "samples": []}
        )
        bucket["count"] += 1
        bucket["source_files"].add(source_file)
        if len(bucket["samples"]) < SAMPLE_LIMIT:
            bucket["samples"].append(
                {
                    "id": issue_id,
                    "source_file": source_file,
                    "source_key": source_key,
                    "reason": reason,
                    "payload": _anonymize(payload or {}),
                }
            )
    for bucket in by_category.values():
        bucket["source_files"] = sorted(bucket["source_files"])

    return {
        "summary": {
            "players_total": players_total,
            "players_linked": players_linked,
            "players_unlinked": players_total - players_linked,
            "open_issues": len(issue_rows),
            "issue_categories": sorted(by_category),
        },
        "unlinked_players": [
            {"id": row[0], "ign": row[1]} for row in unlinked_rows
        ],
        "open_issues_by_category": by_category,
        "recommendation": REPORT_RECOMMENDATION,
    }
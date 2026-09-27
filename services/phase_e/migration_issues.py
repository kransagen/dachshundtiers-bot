"""Phase E, E9 — migration issues classified into 6 actionable buckets.

The 13 granular ``MigrationImportIssue`` categories produced by the legacy
JSON import are grouped into 6 operational buckets. This report NEVER
resolves anything: no automatic retry, no fuzzy match, no Discord mutation.
The ONLY resolution paths are:

- explicit ``/linkdiscord`` (admin-verified identity claim);
- correcting the legacy source file and re-running the idempotent import.

Each bucket carries an explicit, human-actionable recommendation and lists
its source files + anonymized samples so an operator can act on it without
guessing identities.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MigrationImportIssue

from services.phase_d.import_data import (
    CAT_COOLDOWN_INVALID_EXPIRY,
    CAT_COOLDOWN_INVALID_KEY,
    CAT_COOLDOWN_KIT_UNKNOWN,
    CAT_COOLDOWN_KIT_UNATTRIBUTABLE,
    CAT_COOLDOWN_UNRESOLVED,
    CAT_HISTORY_INVALID_DATE,
    CAT_HISTORY_UNKNOWN_KIT,
    CAT_HISTORY_UNKNOWN_TIER,
    CAT_HT3_INVALID_KEY,
    CAT_HT3_UNKNOWN_KIT,
    CAT_HT3_UNRESOLVED,
    CAT_PLAYER_MALFORMED,
    CAT_PLAYER_NO_USERNAME,
    CAT_TESTER_INVALID_KEY,
    CAT_TESTER_UNRESOLVED,
)

SAMPLE_LIMIT = 3

GROUP_PLAYER = "player_record"
GROUP_HISTORY = "player_history"
GROUP_COOLDOWN = "cooldown"
GROUP_HT3 = "ht3_cooldown"
GROUP_TESTER = "tester"
GROUP_OTHER = "other"

BUCKET_PLAYER = {
    "name": "Poškozené záznamy hráčů",
    "categories": [CAT_PLAYER_MALFORMED, CAT_PLAYER_NO_USERNAME],
    "action": (
        "Opravit zdrojový players.json (doplnit username / opravit strukturu) "
        "a znovu spustit idempotentní import. Žádné automatické doplnění identity."
    ),
}
BUCKET_HISTORY = {
    "name": "Historie s neznámými hodnotami",
    "categories": [
        CAT_HISTORY_INVALID_DATE,
        CAT_HISTORY_UNKNOWN_KIT,
        CAT_HISTORY_UNKNOWN_TIER,
    ],
    "action": (
        "Doplnit chybějící kit/tier definici nebo opravit datum v players.json; "
        "nikdy nehádat tier. Znovu spustit import."
    ),
}
BUCKET_COOLDOWN = {
    "name": "Cooldowny bez propojení",
    "categories": [
        CAT_COOLDOWN_UNRESOLVED,
        CAT_COOLDOWN_INVALID_KEY,
        CAT_COOLDOWN_INVALID_EXPIRY,
        CAT_COOLDOWN_KIT_UNATTRIBUTABLE,
        CAT_COOLDOWN_KIT_UNKNOWN,
    ],
    "action": (
        "Přiřadit hráče JEN explicitním /linkdiscord nebo opravit klíč/expiraci "
        "v cooldowns.json. Neúplné záznamy zůstávají otevřené. Cooldown bez jednoznačného "
        "kitu (category cooldown_kit_unattributable / cooldown_kit_unknown_in_registry) "
        "zůstává záměrně globální a do vypršení blokuje všechny kity — opravit nebo "
        "explicitně zrušit, žádný kit se nehádá."
    ),
}
BUCKET_HT3 = {
    "name": "HT3 cooldowny bez propojení",
    "categories": [
        CAT_HT3_UNRESOLVED,
        CAT_HT3_UNKNOWN_KIT,
        CAT_HT3_INVALID_KEY,
    ],
    "action": (
        "Přiřadit hráče JEN explicitním /linkdiscord nebo doplnit kit / opravit "
        "klíč v ht3_cooldowns.json. Bez hádek."
    ),
}
BUCKET_TESTER = {
    "name": "Testeři bez propojení / neplatné klíče",
    "categories": [CAT_TESTER_UNRESOLVED, CAT_TESTER_INVALID_KEY],
    "action": (
        "Přiřadit hráče JEN explicitním /linkdiscord nebo opravit klíč "
        "v testers.json."
    ),
}
BUCKET_OTHER = {
    "name": "Neznámé kategorie (budoucí)",
    "categories": [],
    "action": (
        "Nové kategorie se nikdy nevynechávají: vždy se zařadí sem a vyžadují "
        "explicitní rozhodnutí operátora."
    ),
}

BUCKETS = [
    BUCKET_PLAYER,
    BUCKET_HISTORY,
    BUCKET_COOLDOWN,
    BUCKET_HT3,
    BUCKET_TESTER,
    BUCKET_OTHER,
]

ACTIONABLE_RECOMMENDATION = (
    "Žádný záznam se neřeší automaticky. Jediné akce: (1) explicitní "
    "/linkdiscord ke spárování identity, (2) oprava zdrojového JSON a opětovný "
    "idempotentní import, (3) explicitní operátorské rozhodnutí. Do té doby "
    "zůstává issue otevřené a stav je evidentně nedořešený – nikdy se neskrývá."
)


def _anonymize(payload: dict | None) -> dict:
    return {k: str(v)[:120] for k, v in list((payload or {}).items())[:4]}


async def _bucket_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(
                MigrationImportIssue.category,
                func.count(MigrationImportIssue.id),
            )
            .where(MigrationImportIssue.status == "open")
            .group_by(MigrationImportIssue.category)
        )
    ).all()
    counts = {category: 0 for bucket in BUCKETS for category in bucket["categories"]}
    for category, count in rows:
        counts[category] = count
    return counts


async def _bucket_rows(
    session: AsyncSession, categories: list[str], limit: int = 50
) -> list:
    if not categories:
        return []
    return (
        (
            await session.execute(
                select(
                    MigrationImportIssue.id,
                    MigrationImportIssue.category,
                    MigrationImportIssue.source_file,
                    MigrationImportIssue.source_key,
                    MigrationImportIssue.reason,
                    MigrationImportIssue.payload,
                )
                .where(
                    MigrationImportIssue.status == "open",
                    MigrationImportIssue.category.in_(categories),
                )
                .order_by(MigrationImportIssue.id)
                .limit(limit)
            )
        )
        .all()
    )


def _anonymize(payload: dict | None) -> dict:
    return {k: str(v)[:120] for k, v in list((payload or {}).items())[:4]}


def _samples(rows: list, limit: int = SAMPLE_LIMIT) -> list[dict]:
    return [
        {
            "id": row[0],
            "category": row[1],
            "source_file": row[2],
            "source_key": row[3],
            "reason": row[4],
            "payload": _anonymize(row[5]),
        }
        for row in rows[:limit]
    ]


def _source_files(rows: list) -> list[str]:
    return sorted({row[2] for row in rows})


async def build_migration_issues_report(session_factory) -> dict:
    """Read-only classification: groups open issues into 6 actionable buckets.

    Never writes, never resolves, never guesses. Report structure is stable
    so the existence (and non-resolution) of each bucket is verifiable.
    """
    async with session_factory() as session:
        counts = await _bucket_counts(session)
        total = sum(category_count for category_count in counts.values())
        buckets = []
        seen_categories = set()
        for bucket in BUCKETS:
            seen_categories.update(bucket["categories"])
            bucket_counts = {
                cat: counts.get(cat, 0) for cat in bucket["categories"]
            }
            rows = await _bucket_rows(session, bucket["categories"])
            buckets.append(
                {
                    "name": bucket["name"],
                    "count": sum(bucket_counts.values()),
                    "categories": bucket_counts,
                    "source_files": _source_files(rows),
                    "samples": _samples(rows),
                    "action": bucket["action"],
                }
            )
        other_categories = sorted(set(counts) - seen_categories)
        if other_categories:
            # Future categories land in "other" — still reported, still open.
            other_rows = await _bucket_rows(session, other_categories)
            other_bucket = next(b for b in buckets if b["name"] == BUCKET_OTHER["name"])
            other_bucket["categories"] = {
                cat: counts[cat] for cat in other_categories
            }
            other_bucket["count"] = sum(counts[cat] for cat in other_categories)
            other_bucket["samples"] = _samples(other_rows)
            other_bucket["source_files"] = _source_files(other_rows)

    return {
        "summary": {
            "open_issues": total,
            "bucket_count": 6,
            "buckets": [b["name"] for b in buckets],
        },
        "buckets": buckets,
        "recommendation": ACTIONABLE_RECOMMENDATION,
    }
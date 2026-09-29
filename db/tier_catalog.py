"""Canonical tier catalogue: ladder order, ranks and retired variants.

The ladder (worst → best) is ``LT5 < HT5 < LT4 < HT4 < LT3 < HT3 < LT2 <
HT2 < LT1 < HT1``. ``rank`` in ``tier_definitions`` is the 1-based position
in that ladder, so "one ladder step lower" is simply ``rank - 1``.

Retired tiers are ``R`` + ladder code (``RLT2``) with ``kind='retired'``,
``is_retired=True`` and ``retired_of_id`` pointing at the ladder tier.

``LT3E`` (LT3 + eval) is a virtual status, not a ladder step.
"""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

LADDER: tuple[str, ...] = (
    "LT5", "HT5", "LT4", "HT4", "LT3", "HT3", "LT2", "HT2", "LT1", "HT1",
)
RANK: dict[str, int] = {code: i + 1 for i, code in enumerate(LADDER)}
RETIRED_PREFIX = "R"
VIRTUAL_EVAL = "LT3E"

_RETIRED_RE = re.compile(r"^R((?:LT|HT)[1-5])$")


def ladder_rank(code: Optional[str]) -> Optional[int]:
    return RANK.get((code or "").strip().upper())


def retired_code(code: str) -> str:
    return RETIRED_PREFIX + code.strip().upper()


def retired_base(code: Optional[str]) -> Optional[str]:
    """``RLT2`` → ``LT2``; ``None`` for anything that is not a retired code."""
    match = _RETIRED_RE.match((code or "").strip().upper())
    return match.group(1) if match else None


def is_retired_code(code: Optional[str]) -> bool:
    return retired_base(code) is not None


def classify_code(code: str) -> tuple[str, Optional[int]]:
    """(kind, rank) for a tier code — ``ladder`` / ``retired`` / ``virtual``."""
    clean = code.strip().upper()
    if clean in RANK:
        return "ladder", RANK[clean]
    if is_retired_code(clean):
        return "retired", None
    return "virtual", None


async def ensure_tier(session: AsyncSession, code: str):
    """Get-or-create one tier definition with the canonical kind/rank.

    Existing rows are only *completed*, never re-purposed: a ladder tier
    without a rank gets it, a retired code imported earlier as ``virtual``
    becomes ``retired`` with ``retired_of_id``. Anything else is left alone.
    """
    from db.repositories.kits import TierDefinitionRepository

    repo = TierDefinitionRepository()
    clean = code.strip()
    upper = clean.upper()
    kind, rank = classify_code(upper)
    lookup = upper if kind in ("ladder", "retired") else clean
    tier = await repo.get_by_code(session, lookup)
    base = None
    if kind == "retired":
        base = await ensure_tier(session, retired_base(upper))
    if tier is None:
        return await repo.get_or_create(
            session,
            code=lookup,
            kind=kind,
            display_name=lookup,
            rank=rank,
            is_retired=kind == "retired",
            retired_of_id=base.id if base is not None else None,
        )
    changed = False
    if kind == "ladder" and tier.rank is None:
        tier.rank = rank
        changed = True
    if kind == "retired" and (tier.kind != "retired" or tier.retired_of_id is None):
        tier.kind = "retired"
        tier.is_retired = True
        tier.retired_of_id = base.id
        changed = True
    if changed:
        await session.flush()
    return tier


async def ensure_canonical_tiers(session: AsyncSession) -> None:
    """All ladder tiers and their retired variants exist with kind/rank."""
    for code in LADDER:
        await ensure_tier(session, code)
        await ensure_tier(session, retired_code(code))

"""D2b — idempotent relational import of the legacy ``kit_roles.json``.

``import_data.py`` (D2) deliberately never touched ``kit_roles.json`` — it is
role-mapping *configuration*, not player/tier history. But the PostgreSQL
``kit_roles`` table has to be non-empty before the bot will start in DB mode
(``db.services.config_validation.validate_kit_role_configuration`` fails fast
on zero mappings), so a fresh PostgreSQL cutover needs this run once too.

Format of ``data/kit_roles.json`` (written by ``/setkitrole``, see
``services/kit_roles.py``): ``{kit_key: {tier_code: "discord_role_id"}}``.

Idempotent: kits/tiers use get-or-create, ``KitRoleRepository.set_mapping``
upserts on the ``(kit_id, tier_id)`` unique constraint. A second run on the
same database changes nothing. Runs in one transaction, same as D2.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories.kits import KitRepository, KitRoleRepository, TierDefinitionRepository
from db.services.session import transaction

KIT_ROLES_FILE = "kit_roles.json"
_LADDER_CODE_RE = re.compile(r"^R?(LT|HT)[0-9]$")


def _tier_kind(code: str) -> str:
    return "ladder" if _LADDER_CODE_RE.match(code) else "virtual"


def _load_kit_roles(data_dir: Path) -> dict[str, Any]:
    import json

    path = data_dir / KIT_ROLES_FILE
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw if isinstance(raw, dict) else {}


async def run_import(session: AsyncSession, *, data_dir: Path) -> dict:
    raw = _load_kit_roles(data_dir)
    kit_repo = KitRepository()
    tier_repo = TierDefinitionRepository()
    role_repo = KitRoleRepository()

    report: dict[str, Any] = {
        "source_file": str(data_dir / KIT_ROLES_FILE),
        "kits_seen": 0,
        "mappings_imported": 0,
        "skipped_invalid": [],
    }

    for kit_key, tier_map in raw.items():
        if not isinstance(tier_map, dict) or not str(kit_key).strip():
            report["skipped_invalid"].append({"kit": kit_key, "reason": "not a mapping"})
            continue
        key = str(kit_key).strip().lower()
        kit = await kit_repo.get_or_create(session, key=key, name=str(kit_key))
        report["kits_seen"] += 1
        for tier_code, role_id in tier_map.items():
            code = str(tier_code).strip().upper()
            if not code:
                continue
            try:
                role_id_int = int(role_id)
            except (TypeError, ValueError):
                report["skipped_invalid"].append(
                    {"kit": key, "tier": code, "role_id": role_id, "reason": "non-numeric role id"}
                )
                continue
            tier = await tier_repo.get_or_create(
                session, code=code, kind=_tier_kind(code), display_name=code
            )
            await role_repo.set_mapping(
                session, kit_id=kit.id, tier_id=tier.id, discord_role_id=role_id_int
            )
            report["mappings_imported"] += 1

    return report


async def import_kit_roles_json(session_factory, *, data_dir: Path) -> dict:
    """Public entry: single transaction, commit-or-rollback."""
    async with transaction(session_factory) as session:
        return await run_import(session, data_dir=data_dir)

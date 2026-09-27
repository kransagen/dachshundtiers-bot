"""Mapování rolí kit→tier — nad KitRole (PostgreSQL, jediné úložiště).

Používá se i za runtime čtení při promoci (auto_grant_kit_role). Vrací
slovníky {tier_code: discord_role_id} (int).
"""

from __future__ import annotations

from sqlalchemy import select

from db.models import Kit, KitRole, TierDefinition
from db.repositories.kits import KitRepository, KitRoleRepository
from db.services.session import transaction as db_transaction


async def get_kit_role_map(kit_key: str, *, session_factory) -> dict[str, int]:
    """Mapa {tier_code: role_id} pro kit; prázdná, když neexistuje."""
    key = (kit_key or "").strip().lower()
    if not key:
        return {}
    async with db_transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, key)
        if kit is None:
            return {}
        rows = await session.execute(
            select(KitRole, TierDefinition.code)
            .join(TierDefinition, TierDefinition.id == KitRole.tier_id)
            .where(KitRole.kit_id == kit.id)
        )
    return {code: role.discord_role_id for role, code in rows}


async def get_all_kit_role_maps(*, session_factory) -> dict[str, dict]:
    async with db_transaction(session_factory) as session:
        rows = await session.execute(
            select(Kit, KitRole, TierDefinition.code)
            .join(KitRole, KitRole.kit_id == Kit.id)
            .join(TierDefinition, TierDefinition.id == KitRole.tier_id)
            .order_by(Kit.key, TierDefinition.rank)
        )
    out: dict[str, dict] = {}
    for kit, role, code in rows:
        out.setdefault(kit.key, {})[code] = role.discord_role_id
    return out


async def set_kit_role(
    kit_key: str, tier: str, role_id: int, *, session_factory
) -> bool:
    key = (kit_key or "").strip().lower()
    tier_up = (tier or "").strip().upper()
    if not key or not tier_up:
        return False
    async with db_transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, key)
        tier_row = await session.execute(
            select(TierDefinition).where(TierDefinition.code == tier_up)
        )
        tier_row = tier_row.scalar_one_or_none()
        if kit is None or tier_row is None:
            return False
        await KitRoleRepository().set_mapping(
            session,
            kit_id=kit.id,
            tier_id=tier_row.id,
            discord_role_id=int(role_id),
        )
    return True


async def unset_kit_role(kit_key: str, tier: str, *, session_factory) -> bool:
    key = (kit_key or "").strip().lower()
    tier_up = (tier or "").strip().upper()
    if not key or not tier_up:
        return False
    async with db_transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, key)
        tier_row = await session.execute(
            select(TierDefinition).where(TierDefinition.code == tier_up)
        )
        tier_row = tier_row.scalar_one_or_none()
        if kit is None or tier_row is None:
            return False
        removed = await KitRoleRepository().remove_mapping(
            session, kit_id=kit.id, tier_id=tier_row.id
        )
    return removed > 0
"""Mapování rolí kit→tier (Phase F, todo #9) — dual-mode nad KitRole.

Nahrazuje perzistenci ``data/kit_roles.json`` v produkčních cestách (F10)
— včetně runtime čtení při promoci (auto_grant_kit_role). Dual-mode vzor:
``session_factory`` předán → PostgreSQL, jinak JSON. Vrací slovníky
{tier_code: discord_role_id} — hodnoty v JSON režimu zůstávají řetězce,
v DB režimu int (volající int() sjednocují, viz auto_grant_kit_role).
"""

from __future__ import annotations

from sqlalchemy import select

from db.models import Kit, KitRole, TierDefinition
from db.repositories.kits import KitRepository, KitRoleRepository
from db.services.session import transaction as db_transaction
from storage import load_data, save_data

KIT_ROLES_FILE = "kit_roles.json"


async def get_kit_role_map(
    kit_key: str, *, session_factory=None
) -> dict[str, int]:
    """Mapa {tier_code: role_id} pro kit; prázdná, když neexistuje."""
    key = (kit_key or "").strip().lower()
    if not key:
        return {}
    if session_factory is not None:
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
    roles_map = load_data(KIT_ROLES_FILE, {})
    return dict(roles_map.get(key, {}))


async def get_all_kit_role_maps(*, session_factory=None) -> dict[str, dict]:
    if session_factory is not None:
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
    return load_data(KIT_ROLES_FILE, {})


async def set_kit_role(
    kit_key: str, tier: str, role_id: int, *, session_factory=None
) -> bool:
    key = (kit_key or "").strip().lower()
    tier_up = (tier or "").strip().upper()
    if not key or not tier_up:
        return False
    if session_factory is not None:
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
    roles_map = load_data(KIT_ROLES_FILE, {})
    roles_map.setdefault(key, {})[tier_up] = str(int(role_id))
    save_data(KIT_ROLES_FILE, roles_map)
    return True


async def unset_kit_role(
    kit_key: str, tier: str, *, session_factory=None
) -> bool:
    key = (kit_key or "").strip().lower()
    tier_up = (tier or "").strip().upper()
    if not key or not tier_up:
        return False
    if session_factory is not None:
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
    roles_map = load_data(KIT_ROLES_FILE, {})
    kit_map = roles_map.get(key, {})
    if tier_up not in kit_map:
        return False
    del kit_map[tier_up]
    if not kit_map:
        roles_map.pop(key, None)
    save_data(KIT_ROLES_FILE, roles_map)
    return True
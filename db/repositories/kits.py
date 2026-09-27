"""Kit / tier-definition / kit-role (Discord role mapping) repositories."""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Kit, KitRole, TierDefinition


class KitRepository:
    """Dimensions: kits (ladder/tournament groupings)."""

    async def get_by_key(self, session: AsyncSession, key: str) -> Optional[Kit]:
        result = await session.execute(select(Kit).where(Kit.key == key))
        return result.scalar_one_or_none()

    async def get_by_name(self, session: AsyncSession, name: str) -> Optional[Kit]:
        """Display-name lookup (case-insensitive); ``Kit.name`` je unikátní."""
        value = (name or "").strip().lower()
        if not value:
            return None
        result = await session.execute(
            select(Kit).where(func.lower(Kit.name) == value)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session: AsyncSession, kit_id: int) -> Optional[Kit]:
        return await session.get(Kit, kit_id)

    async def get_or_create(
        self, session: AsyncSession, *, key: str, name: str, active: bool = True
    ) -> Kit:
        kit = await self.get_by_key(session, key)
        if kit is not None:
            return kit
        kit = Kit(key=key, name=name, active=active)
        session.add(kit)
        await session.flush()
        return kit

    async def list(self, session: AsyncSession, *, only_active: bool = True) -> list[Kit]:
        stmt = select(Kit)
        if only_active:
            stmt = stmt.where(Kit.active.is_(True))
        result = await session.execute(stmt.order_by(Kit.name))
        return list(result.scalars())


class TierDefinitionRepository:
    """Tier catalogue; retired tiers redirect through ``retired_of_id``."""

    async def get_by_code(self, session: AsyncSession, code: str) -> Optional[TierDefinition]:
        result = await session.execute(select(TierDefinition).where(TierDefinition.code == code))
        return result.scalar_one_or_none()

    async def get_by_id(
        self, session: AsyncSession, tier_id: int
    ) -> Optional[TierDefinition]:
        return await session.get(TierDefinition, tier_id)

    async def get_or_create(
        self,
        session: AsyncSession,
        *,
        code: str,
        kind: str,
        display_name: str,
        rank: Optional[int] = None,
        is_retired: bool = False,
        retired_of_id: Optional[int] = None,
    ) -> TierDefinition:
        tier = await self.get_by_code(session, code)
        if tier is not None:
            return tier
        tier = TierDefinition(
            code=code,
            kind=kind,
            display_name=display_name,
            rank=rank,
            is_retired=is_retired,
            retired_of_id=retired_of_id,
        )
        session.add(tier)
        await session.flush()
        return tier

    async def list(
        self, session: AsyncSession, *, kind: Optional[str] = None
    ) -> list[TierDefinition]:
        stmt = select(TierDefinition)
        if kind is not None:
            stmt = stmt.where(TierDefinition.kind == kind)
        result = await session.execute(stmt.order_by(TierDefinition.rank.asc().nulls_last(), TierDefinition.code))
        return list(result.scalars())


class KitRoleRepository:
    """Discord role <-> (kit, tier) mapping — the only role identifiers the
    mirror observes. ``discord_role_id`` is unique; a role can never map to
    two (kit, tier) pairs."""

    async def set_mapping(
        self,
        session: AsyncSession,
        *,
        kit_id: int,
        tier_id: int,
        discord_role_id: int,
    ) -> KitRole:
        role_id = int(discord_role_id)
        stmt = (
            pg_insert(KitRole)
            .values(kit_id=kit_id, tier_id=tier_id, discord_role_id=role_id)
            .on_conflict_do_update(
                constraint="uq_kit_roles_kit_tier",
                set_={"discord_role_id": role_id},
            )
            .returning(KitRole)
            .execution_options(populate_existing=True)
        )
        row = (await session.execute(stmt)).scalar_one()
        await session.flush()
        return row

    async def remove_mapping(
        self,
        session: AsyncSession,
        *,
        kit_id: Optional[int] = None,
        tier_id: Optional[int] = None,
        discord_role_id: Optional[int] = None,
    ) -> int:
        stmt = delete(KitRole)
        if kit_id is not None:
            stmt = stmt.where(KitRole.kit_id == kit_id)
        if tier_id is not None:
            stmt = stmt.where(KitRole.tier_id == tier_id)
        if discord_role_id is not None:
            stmt = stmt.where(KitRole.discord_role_id == discord_role_id)
        result = await session.execute(stmt)
        return result.rowcount or 0

    async def get_all(self, session: AsyncSession) -> list[KitRole]:
        result = await session.execute(select(KitRole).order_by(KitRole.kit_id, KitRole.tier_id))
        return list(result.scalars())

    async def get_by_role(
        self, session: AsyncSession, discord_role_id: int
    ) -> Optional[KitRole]:
        result = await session.execute(
            select(KitRole).where(KitRole.discord_role_id == int(discord_role_id))
        )
        return result.scalar_one_or_none()

    async def role_snapshot(
        self, session: AsyncSession
    ) -> dict[int, tuple[int, int]]:
        """role_id -> (kit_id, tier_id); used by the pure classifier."""
        roles = await self.get_all(session)
        return {r.discord_role_id: (r.kit_id, r.tier_id) for r in roles}

    async def mappings_for_kit(
        self, session: AsyncSession, kit_id: int
    ) -> list[KitRole]:
        result = await session.execute(
            select(KitRole).where(KitRole.kit_id == kit_id).order_by(KitRole.tier_id)
        )
        return list(result.scalars())


async def ensure_dimensions(
    session: AsyncSession,
    kits: Iterable[tuple[str, str]],
    tier_defs: Iterable[tuple[str, str, str, Optional[int]]],
) -> None:
    """Seed convenience: kits (key, name) and tiers (code, kind, display, rank)."""
    kit_repo = KitRepository()
    tier_repo = TierDefinitionRepository()
    for key, name in kits:
        await kit_repo.get_or_create(session, key=key, name=name)
    for code, kind, display_name, rank in tier_defs:
        await tier_repo.get_or_create(
            session, code=code, kind=kind, display_name=display_name, rank=rank
        )
    await session.flush()
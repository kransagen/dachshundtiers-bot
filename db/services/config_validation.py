"""Kit-role configuration validation (PostgreSQL-backed equivalent of
``db.validation``): the bot must not run with an incomplete/mismatched
Discord role mapping — FAIL FAST (invariant: "If required role configuration
is missing: FAIL FAST")."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from db.repositories.kits import KitRepository, KitRoleRepository

STRICT_KIT_ROLES_ENV = "STRICT_KIT_ROLES"


class KitRoleConfigError(ValueError):
    """Missing/misconfigured kit-role mapping — the bot must not continue."""


@dataclass(frozen=True)
class KitRoleValidation:
    mapping_count: int
    kits_without_mapping: tuple[str, ...]
    warnings: tuple[str, ...] = ()


async def validate_kit_role_configuration(
    session: AsyncSession,
    *,
    guild_role_ids: set[int],
    strict_kits: bool = False,
) -> KitRoleValidation:
    kits = await KitRepository().list(session)
    mappings = await KitRoleRepository().get_all(session)

    if not mappings:
        raise KitRoleConfigError("Žádné přiřazení kit-tier→Discord role v databázi.")

    mapped_roles: set[int] = set()
    mapped_kits: set[int] = set()
    for mapping in mappings:
        role_id = mapping.discord_role_id
        if not isinstance(role_id, int):
            raise KitRoleConfigError(
                f"Nenumerická Discord role v kit_roles: {role_id!r}"
            )
        mapped_roles.add(role_id)
        mapped_kits.add(mapping.kit_id)

    missing_from_guild = sorted(mapped_roles - guild_role_ids)
    if missing_from_guild:
        raise KitRoleConfigError(
            "Následující Discord role přiřazené v databázi nejsou v gildě: "
            f"{missing_from_guild}"
        )

    warnings: list[str] = []
    kits_without_mapping = tuple(
        kit.key for kit in kits if kit.id not in mapped_kits
    )
    if kits_without_mapping:
        message = (
            "Kity bez přiřazených Discord rolí: "
            f"{', '.join(kits_without_mapping)}"
        )
        if strict_kits:
            raise KitRoleConfigError(message)
        warnings.append(message)

    return KitRoleValidation(
        mapping_count=len(mappings),
        kits_without_mapping=kits_without_mapping,
        warnings=tuple(warnings),
    )
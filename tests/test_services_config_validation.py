"""Kit-role configuration validation service tests."""

import pytest
from sqlalchemy import select

from db.models import Kit, TierDefinition
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.services.config_validation import (
    KitRoleConfigError,
    validate_kit_role_configuration,
)
from db.services.session import transaction


async def _seed_mapping(session_factory, *, role_ids=(5001, 5002), kits=("ht3",)):
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            tuple((k, k.title()) for k in kits),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        tiers = {
            t.code: t
            for t in (await session.execute(
                select(TierDefinition).where(TierDefinition.kind == "ladder")
            )).scalars()
        }
        repo = KitRoleRepository()
        await repo.set_mapping(session, kit_id=kit.id, tier_id=tiers["t1"].id, discord_role_id=role_ids[0])
        await repo.set_mapping(session, kit_id=kit.id, tier_id=tiers["t2"].id, discord_role_id=role_ids[1])


async def test_empty_mapping_fails_fast(session_factory, clean_db):
    with pytest.raises(KitRoleConfigError):
        async with transaction(session_factory) as session:
            await validate_kit_role_configuration(
                session, guild_role_ids={5001, 5002}
            )


async def test_missing_guild_role_fails_fast(session_factory, clean_db):
    await _seed_mapping(session_factory)
    with pytest.raises(KitRoleConfigError) as exc:
        async with transaction(session_factory) as session:
            await validate_kit_role_configuration(
                session, guild_role_ids={5001}
            )
    assert "5002" in str(exc.value)


async def test_valid_configuration_passes(session_factory, clean_db):
    await _seed_mapping(session_factory)
    async with transaction(session_factory) as session:
        result = await validate_kit_role_configuration(
            session, guild_role_ids={5001, 5002}
        )
    assert result.mapping_count == 2
    assert result.warnings == ()


async def test_kit_without_mapping_warns_unless_strict(session_factory, clean_db):
    await _seed_mapping(session_factory, role_ids=(6001, 6002), kits=("ht3", "tourney"))
    async with transaction(session_factory) as session:
        result = await validate_kit_role_configuration(
            session, guild_role_ids={6001, 6002}
        )
    assert result.kits_without_mapping == ("tourney",)
    with pytest.raises(KitRoleConfigError):
        async with transaction(session_factory) as session:
            await validate_kit_role_configuration(
                session, guild_role_ids={6001, 6002}, strict_kits=True
            )


async def test_extra_guild_roles_are_ignored(session_factory, clean_db):
    await _seed_mapping(session_factory)
    async with transaction(session_factory) as session:
        result = await validate_kit_role_configuration(
            session, guild_role_ids={5001, 5002, 9999}
        )
    assert result.mapping_count == 2
    assert result.warnings == ()
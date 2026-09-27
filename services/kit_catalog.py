"""Katalog kitů — nad Kit/KitRepository (PostgreSQL, jediné úložiště).

Prázdný katalog fallbackuje na DEFAULT_KITS (výchozí sada, dokud se neobjeví
první vlastní kit). Volající bez ``session_factory`` (staré cesty ve
velkých, zatím nepřevedených cogs) dostanou stejný bezpečný prázdný/default
výsledek jako prázdná DB – ŽÁDNÝ soubor se přitom nečte ani nezapisuje.
"""

from __future__ import annotations

import discord
from discord import app_commands

from db.repositories.kits import KitRepository
from db.services.session import transaction as db_transaction
from utils import DEFAULT_KITS


async def get_kits(*, session_factory=None) -> list[str]:
    if session_factory is None:
        return list(DEFAULT_KITS)
    async with db_transaction(session_factory) as session:
        active = await KitRepository().list(session)
        if not active and not await KitRepository().list(
            session, only_active=False
        ):
            return list(DEFAULT_KITS)
    return [kit.name for kit in active]


async def canonical_kit_name(kit: str, *, session_factory=None) -> str:
    value = (kit or "").strip()
    if not value or session_factory is None:
        return value
    async with db_transaction(session_factory) as session:
        found = await KitRepository().get_by_name(session, value)
    return found.name if found is not None else value


async def add_kit(kit: str, *, session_factory=None) -> bool:
    kit = kit.strip()
    if not kit:
        return False
    if session_factory is None:
        return False
    async with db_transaction(session_factory) as session:
        repo = KitRepository()
        existing = await repo.get_by_name(session, kit)
        if existing is not None:
            if existing.active:
                return False
            existing.active = True
            existing.name = kit
            await session.flush()
            return True
        await repo.get_or_create(session, key=kit.lower(), name=kit, active=True)
    return True


async def remove_kit(kit: str, *, session_factory=None) -> bool:
    kit = kit.strip().lower()
    if not kit or session_factory is None:
        return False
    async with db_transaction(session_factory) as session:
        found = await KitRepository().get_by_name(session, kit)
        if found is None or not found.active:
            return False
        found.active = False
        await session.flush()
    return True


async def kit_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete názvů kitů."""
    session_factory = getattr(
        getattr(interaction, "client", None), "db_session_factory", None
    )
    kits = await get_kits(session_factory=session_factory)
    if current:
        kits = [k for k in kits if current.lower() in k.lower()]
    return [app_commands.Choice(name=k, value=k) for k in kits[:25]]
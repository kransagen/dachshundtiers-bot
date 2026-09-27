"""Katalog kitů (Phase F, todo #9) — dual-mode nad Kit/KitRepository.

Nahrazuje perzistenci ``data/kits.json`` (utils.add_kit/get_kits/remove_kit)
v produkčních exekučních cestách (F10). Dual-mode vzor: ``session_factory``
předán → PostgreSQL, jinak původní JSON soubor (= utils funkce beze změny).
Prázdný DB katalog fallbackuje na DEFAULT_KITS — parity s JSON režimem, kdy
soubor neexistuje (výchozí slovníček, dokud se neobjeví první vlastní kit).
"""

from __future__ import annotations

import discord
from discord import app_commands

from db.repositories.kits import KitRepository
from db.services.session import transaction as db_transaction
from utils import (
    DEFAULT_KITS,
    add_kit as _json_add_kit,
    canonical_kit_name as _json_canonical,
    get_kits as _json_get_kits,
    remove_kit as _json_remove_kit,
)


async def get_kits(*, session_factory=None) -> list[str]:
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            active = await KitRepository().list(session)
            if not active and not await KitRepository().list(
                session, only_active=False
            ):
                return list(DEFAULT_KITS)
        return [kit.name for kit in active]
    return _json_get_kits()


async def canonical_kit_name(kit: str, *, session_factory=None) -> str:
    value = (kit or "").strip()
    if not value:
        return value
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            found = await KitRepository().get_by_name(session, value)
        return found.name if found is not None else value
    return _json_canonical(kit)


async def add_kit(kit: str, *, session_factory=None) -> bool:
    kit = kit.strip()
    if not kit:
        return False
    if session_factory is not None:
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
            await repo.get_or_create(
                session, key=kit.lower(), name=kit, active=True
            )
        return True
    return _json_add_kit(kit)


async def remove_kit(kit: str, *, session_factory=None) -> bool:
    kit = kit.strip().lower()
    if not kit:
        return False
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            found = await KitRepository().get_by_name(session, kit)
            if found is None or not found.active:
                return False
            found.active = False
            await session.flush()
        return True
    return _json_remove_kit(kit)


async def kit_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete názvů kitů; DB režim se odvodí z interakčního clienta."""
    session_factory = getattr(
        getattr(interaction, "client", None), "db_session_factory", None
    )
    kits = await get_kits(session_factory=session_factory)
    if current:
        kits = [k for k in kits if current.lower() in k.lower()]
    return [app_commands.Choice(name=k, value=k) for k in kits[:25]]
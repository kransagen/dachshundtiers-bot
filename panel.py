"""Živý waitlist panel – embed a funkce pro automatickou aktualizaci."""

import asyncio
import logging

import discord

from services.config_store import get_queue_channel_id
from services.queue_service import list_queue_entries, panel_message_id, queue_state

log = logging.getLogger("dachshundtiers")

EMBED_DESCRIPTION_LIMIT = 4096
TESTERS_BUDGET = 800

# (loop id, kit_key) -> asyncio.Lock; souběžné překreslení stejného panelu
# se serializuje, takže poslední zápis vždy odpovídá nejnovějšímu stavu.
_panel_locks: "dict[tuple[int, str], asyncio.Lock]" = {}


def _panel_lock(kit_key: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), kit_key)
    lock = _panel_locks.get(key)
    if lock is None:
        lock = _panel_locks[key] = asyncio.Lock()
    return lock


def _numbered_mentions(ids, budget: int) -> str:
    """Očíslované zmínky, oříznuté tak, aby se vešly do ``budget`` znaků."""
    ids = list(ids)
    text = ""
    for i, user_id in enumerate(ids, 1):
        line = f"{i}. <@{user_id}>\n"
        remaining = len(ids) - i + 1
        if len(text) + len(line) > budget - 40 and remaining > 1:
            return text + f"*…a dalších {remaining}*\n"
        text += line
    return text


def create_queue_embed(kit_name: str, current_queue, testers_list) -> discord.Embed:
    """Vytvoří embed odpovídající nasazenému originálu (živý panel fronty)."""
    header = (
        "⏱️ Fronta se aktualizuje automaticky.\n"
        "Použij tlačítka níže pro přidání nebo odebrání.\n\n"
        "**Fronta**:\n"
    )
    testers_text = "\n**Aktivní Testeři**:\n" + _numbered_mentions(
        testers_list, TESTERS_BUDGET
    )

    if not current_queue:
        queue_text = "*Fronta je prázdná. Buď první!*"
    else:
        budget = EMBED_DESCRIPTION_LIMIT - len(header) - len(testers_text)
        queue_text = _numbered_mentions((p.get("id") for p in current_queue), budget)

    return discord.Embed(
        title=f"📝 {kit_name} Waitlist",
        description=header + queue_text + testers_text,
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )


async def update_panel(guild, kit_key: str, *, session_factory) -> None:
    """Aktualizuje embed živého panelu pro daný kit.

    Panel se hledá v určeném kanálu kitu (QUEUE_CHANNELS), ne v kanálu příkazu.
    """
    if guild is None:
        return

    async with _panel_lock(kit_key):
        active, entries, message_id, channel_id = await asyncio.gather(
            queue_state(kit_key, session_factory=session_factory),
            list_queue_entries(kit_key, session_factory=session_factory),
            panel_message_id(kit_key, session_factory=session_factory),
            get_queue_channel_id(kit_key, session_factory=session_factory),
        )
        kit_name = active.get("name", kit_key) if active else kit_key
        testers = active.get("testers", []) if active else []

        if not message_id or not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                return
            except (discord.Forbidden, discord.HTTPException) as err:
                log.warning("Panel %s: kanál %s nelze načíst: %s", kit_key, channel_id, err)
                return

        try:
            message = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            return
        except discord.HTTPException as err:
            log.warning("Panel %s: zprávu %s nelze načíst: %s", kit_key, message_id, err)
            return

        embed = create_queue_embed(kit_name, entries, testers)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException as err:
            log.warning("Panel %s: úprava zprávy selhala: %s", kit_key, err)

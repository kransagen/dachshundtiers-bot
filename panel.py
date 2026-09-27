"""Živý waitlist panel – embed a funkce pro automatickou aktualizaci."""

import discord

from services.config_store import get_queue_channel_id
from services.queue_service import list_queue_entries, panel_message_id, queue_state


def create_queue_embed(kit_name: str, current_queue, testers_list) -> discord.Embed:
    """Vytvoří embed odpovídající nasazenému originálu (živý panel fronty)."""
    description = (
        "⏱️ Fronta se aktualizuje automaticky.\n"
        "Použij tlačítka níže pro přidání nebo odebrání.\n\n"
        "**Fronta**:\n"
    )

    if not current_queue:
        description += "*Fronta je prázdná. Buď první!*"
    else:
        for i, player in enumerate(current_queue, 1):
            description += f"{i}. <@{player.get('id')}>\n"

    description += "\n**Aktivní Testeři**:\n"
    for i, tester_id in enumerate(testers_list, 1):
        description += f"{i}. <@{tester_id}>\n"

    return discord.Embed(
        title=f"📝 {kit_name} Waitlist",
        description=description,
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )


async def update_panel(guild, kit_key: str, *, session_factory) -> None:
    """Aktualizuje embed živého panelu pro daný kit.

    Panel se hledá v určeném kanálu kitu (QUEUE_CHANNELS), ne v kanálu příkazu.
    """
    if guild is None:
        return

    active = await queue_state(kit_key, session_factory=session_factory)
    entries = await list_queue_entries(kit_key, session_factory=session_factory)
    message_id = await panel_message_id(kit_key, session_factory=session_factory)
    channel_id = await get_queue_channel_id(kit_key, session_factory=session_factory)
    kit_name = active.get("name", kit_key) if active else kit_key
    testers = active.get("testers", []) if active else []

    if not message_id or not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.HTTPException):
        return

    embed = create_queue_embed(kit_name, entries, testers)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass
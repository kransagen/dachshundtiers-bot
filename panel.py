"""Živý waitlist panel – embed a funkce pro automatickou aktualizaci."""

import discord

from storage import load_data


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


async def update_panel(channel, kit_key: str) -> None:
    """Aktualizuje embed živého panelu pro daný kit (podle uložené zprávy)."""
    queue_messages = load_data("queue_messages.json", {})
    active_queues = load_data("active_queues.json", {})
    entry = queue_messages.get(kit_key)
    active = active_queues.get(kit_key)

    if not entry or not active:
        return

    # zpětná kompatibilita: entry může být jen ID zprávy (string), nebo slovník
    message_id = entry.get("message_id") if isinstance(entry, dict) else entry
    if not message_id:
        return

    try:
        message = await channel.fetch_message(int(message_id))
    except (discord.NotFound, discord.HTTPException):
        return

    queue = load_data("queue.json")
    filtered = [p for p in queue if str(p.get("kit", "")).lower() == kit_key]
    embed = create_queue_embed(active.get("name", kit_key), filtered, active.get("testers", []))
    try:
        await message.edit(embed=embed)
    except discord.HTTPException:
        pass
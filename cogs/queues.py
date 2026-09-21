"""Cog se správou testovacích front: /openq, /closeq, /queue ..., /removeq.

Port původních funkcí z DACHSHUNDTIERSQBOT (JS):
- /openq  – otevření fronty s živým panelem a tlačítky
- /closeq – uzavření fronty
- /queue join / list / joinastester / joinasqueue / leaveq / pull
- /removeq
"""

import time

import discord
from discord import app_commands
from discord.ext import commands

from config import PLAYER_COOLDOWN_MS
from panel import create_queue_embed, update_panel
from storage import load_data, save_data
from utils import has_tester_role
from views import QueueView


async def _delete_message(channel, message_id) -> None:
    try:
        message = await channel.fetch_message(int(message_id))
        await message.delete()
    except (discord.NotFound, discord.HTTPException):
        pass


class Queues(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /openq
    # ------------------------------------------------------------------
    @app_commands.command(name="openq", description="Open a specific kit queue")
    @app_commands.describe(kit="Name of the kit/queue")
    async def openq(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Only testers can open queues.", ephemeral=True
            )

        kit_key = kit.lower()
        active_queues = load_data("active_queues.json", {})

        if kit_key in active_queues:
            existing = active_queues[kit_key]
            return await interaction.response.send_message(
                f"❌ Pouze jeden tester může přímo inicializovat frontu! "
                f"Queue pro **{existing['name']}** už je otevřená testerem "
                f"<@{existing['opener']}>. Pokud v ní chceš také testovat, "
                f"použij `/queue joinasqueue kit:{kit}`.",
                ephemeral=True,
            )

        # Smaž případný starý panel pro tento kit
        queue_messages = load_data("queue_messages.json", {})
        old = queue_messages.get(kit_key)
        old_id = old.get("message_id") if isinstance(old, dict) else old
        if old_id:
            await _delete_message(interaction.channel, old_id)

        active_queues[kit_key] = {
            "name": kit,
            "opener": str(interaction.user.id),
            "testers": [str(interaction.user.id)],
            "time": time.time() * 1000,
        }
        save_data("active_queues.json", active_queues)

        queue = load_data("queue.json")
        filtered = [p for p in queue if str(p.get("kit", "")).lower() == kit_key]
        embed = create_queue_embed(kit, filtered, active_queues[kit_key]["testers"])

        view = QueueView(kit)
        await interaction.response.send_message("📢 @everyone", embed=embed, view=view)
        message = await interaction.original_response()

        queue_messages[kit_key] = {"message_id": str(message.id), "kit": kit}
        save_data("queue_messages.json", queue_messages)

        # Zaregistrování persistentní view pro restart bota
        self.bot.add_view(view, message_id=message.id)

    # ------------------------------------------------------------------
    # /closeq
    # ------------------------------------------------------------------
    @app_commands.command(name="closeq", description="Close a specific kit queue")
    @app_commands.describe(kit="Name of the kit/queue")
    async def closeq(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Only testers can close queues.", ephemeral=True
            )

        kit_key = kit.lower()
        active_queues = load_data("active_queues.json", {})

        if kit_key not in active_queues:
            return await interaction.response.send_message(
                f"Queue pro **{kit}** není aktivní.", ephemeral=True
            )

        qdata = active_queues[kit_key]

        if len(qdata.get("testers", [])) > 1:
            testers_mention = ", ".join(f"<@{t}>" for t in qdata["testers"])
            return await interaction.response.send_message(
                f"❌ Nemůžeš kompletně zavřít frontu **{qdata['name']}**, protože jsou "
                f"v ní zapsaní další aktivní testeři ({testers_mention}). Musíš nejdříve "
                f"použít `/queue leaveq kit:{kit}`.",
                ephemeral=True,
            )

        del active_queues[kit_key]
        save_data("active_queues.json", active_queues)

        queue_messages = load_data("queue_messages.json", {})
        old = queue_messages.pop(kit_key, None)
        old_id = old.get("message_id") if isinstance(old, dict) else old
        if old_id:
            await _delete_message(interaction.channel, old_id)
        save_data("queue_messages.json", queue_messages)

        closing_ts = int(time.time())
        embed = discord.Embed(
            title=f"🔒 {kit} Queue",
            description=(
                "Queue momentálně není otevřená. Počkej na Testera, který ji otevře.\n\n"
                f"**Naposledy zavřeno**\n<t:{closing_ts}:R>"
            ),
            color=0xEF4444,
            timestamp=discord.utils.utcnow(),
        )

        await interaction.response.send_message(embed=embed, view=QueueView(kit, disabled_join=True))

    # ------------------------------------------------------------------
    # /queue (skupina subpříkazů)
    # ------------------------------------------------------------------
    queue = app_commands.Group(name="queue", description="Manage the testing queue")

    @queue.command(name="join", description="Join the queue for a test")
    @app_commands.describe(ign="Your Minecraft IGN", kit="Kit you want to test")
    async def queue_join(self, interaction: discord.Interaction, ign: str, kit: str) -> None:
        kit_key = kit.lower()
        active_queues = load_data("active_queues.json", {})
        if kit_key not in active_queues:
            return await interaction.response.send_message(
                f"❌ Queue pro kit **{kit}** je momentálně zavřená! Počkej, až ji tester otevře.",
                ephemeral=True,
            )

        user_id = str(interaction.user.id)
        now = time.time() * 1000
        cooldowns = load_data("cooldowns.json", {})
        if user_id in cooldowns and (now - cooldowns[user_id]) < PLAYER_COOLDOWN_MS:
            return await interaction.response.send_message("❌ Máš cooldown na testy!", ephemeral=True)

        queue = load_data("queue.json")
        if any(
            p.get("id") == user_id and str(p.get("kit", "")).lower() == kit_key for p in queue
        ):
            return await interaction.response.send_message(
                "❌ V této frontě už jsi zapsaný.", ephemeral=True
            )

        queue.append(
            {
                "id": user_id,
                "username": interaction.user.name,
                "ign": ign.strip(),
                "kit": kit.strip(),
                "joinedAt": now,
                "testerId": None,
            }
        )
        save_data("queue.json", queue)

        await update_panel(interaction.channel, kit_key)
        await interaction.response.send_message(f"✅ Byl jsi přidán do fronty **{kit.strip()}**.")

    @queue.command(name="list", description="Show the current active queues")
    async def queue_list(self, interaction: discord.Interaction) -> None:
        queue = load_data("queue.json")
        active_queues = load_data("active_queues.json", {})

        embed = discord.Embed(title="📋 Přehled front", color=0xF59E0B)
        description = ""

        for i, p in enumerate(queue, 1):
            description += (
                f"**{i}.** <@{p.get('id')}> (`{p.get('ign')}`) - Kit: **{p.get('kit')}**\n"
            )

        description += "\n**Aktivní fronty a testeři:**\n"
        for kit_key, data in active_queues.items():
            testers = ", ".join(f"<@{t}>" for t in data.get("testers", []))
            testers = testers or f"<@{data.get('opener')}>"
            description += f"• **{data.get('name')}** | Testeri: {testers}\n"

        embed.description = description or "Žádné aktivní zápisy."
        await interaction.response.send_message(embed=embed)

    @queue.command(name="joinastester", description="Register yourself as a globally active tester")
    async def queue_joinastester(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Nemáš roli Tester.", ephemeral=True)

        testers = load_data("testers.json")
        user_id = str(interaction.user.id)
        if user_id not in testers:
            testers.append(user_id)
        save_data("testers.json", testers)

        await interaction.response.send_message(
            f"⚔️ <@{user_id}> je nyní globálně aktivní tester."
        )

    @queue.command(name="joinasqueue", description="Join an already opened queue as an additional tester")
    @app_commands.describe(kit="Name of the active kit")
    async def queue_joinasqueue(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        kit_key = kit.lower()
        active_queues = load_data("active_queues.json", {})
        if kit_key not in active_queues:
            return await interaction.response.send_message(
                "❌ Tato fronta není otevřená!", ephemeral=True
            )

        user_id = str(interaction.user.id)
        if user_id in active_queues[kit_key]["testers"]:
            return await interaction.response.send_message(
                "V této frontě už jsi zapsaný jako aktivní tester.", ephemeral=True
            )

        active_queues[kit_key]["testers"].append(user_id)
        save_data("active_queues.json", active_queues)

        await update_panel(interaction.channel, kit_key)
        await interaction.response.send_message(
            f"⚔️ <@{user_id}> se přidal jako další aktivní tester pro frontu "
            f"**{active_queues[kit_key]['name']}**."
        )

    @queue.command(name="leaveq", description="Leave an active queue you are currently testing in")
    @app_commands.describe(kit="Name of the kit")
    async def queue_leaveq(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        kit_key = kit.lower()
        active_queues = load_data("active_queues.json", {})
        if kit_key not in active_queues:
            return await interaction.response.send_message(
                "Tato fronta neexistuje nebo není aktivní.", ephemeral=True
            )

        user_id = str(interaction.user.id)
        testers = active_queues[kit_key]["testers"]
        if user_id not in testers:
            return await interaction.response.send_message(
                "V této frontě nejsi zapsaný.", ephemeral=True
            )

        testers.remove(user_id)

        # Pokud odchází otevíratel, převezme frontu první tester
        if active_queues[kit_key].get("opener") == user_id and testers:
            active_queues[kit_key]["opener"] = testers[0]

        save_data("active_queues.json", active_queues)

        await update_panel(interaction.channel, kit_key)
        await interaction.response.send_message(
            f"👋 <@{user_id}> opustil frontu **{active_queues[kit_key]['name']}**. "
            "Ostatní testeři mohou pokračovat."
        )

    @queue.command(name="pull", description="Automatically pull the first player")
    async def queue_pull(self, interaction: discord.Interaction) -> None:
        testers = load_data("testers.json")
        if str(interaction.user.id) not in testers:
            return await interaction.response.send_message(
                "❌ Zadej nejdřív `/queue joinastester`", ephemeral=True
            )

        queue = load_data("queue.json")
        if not queue:
            return await interaction.response.send_message("Fronta je prázdná.", ephemeral=True)

        player = queue.pop(0)
        save_data("queue.json", queue)

        embed = (
            discord.Embed(
                title=f"⚔️ Player Pulled for {player.get('kit')}!",
                color=0x5865F2,
            )
            .add_field(
                name="👤 Hráč",
                value=f"<@{player['id']}> (`{player.get('ign')}`)",
                inline=True,
            )
            .add_field(
                name="🛡️ Tester",
                value=f"<@{interaction.user.id}>",
                inline=True,
            )
        )
        await interaction.response.send_message(
            content=f"<@{player['id']}> jsi na řadě!", embed=embed
        )
        await update_panel(interaction.channel, str(player.get("kit", "")).lower())

    # ------------------------------------------------------------------
    # /removeq
    # ------------------------------------------------------------------
    @app_commands.command(name="removeq", description="Manually remove a specific player from the queue")
    @app_commands.describe(hrac="The player you want to remove")
    async def removeq(self, interaction: discord.Interaction, hrac: discord.User) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Chybí oprávnění.", ephemeral=True)

        user_id = str(hrac.id)
        queue = load_data("queue.json")
        entry = next((p for p in queue if p.get("id") == user_id), None)
        if entry is None:
            return await interaction.response.send_message(
                f"❌ Hráč <@{user_id}> nebyl nalezen v žádné aktivní frontě.", ephemeral=True
            )

        queue = [p for p in queue if p.get("id") != user_id]
        save_data("queue.json", queue)

        await interaction.response.send_message(
            f"🧹 Hráč <@{user_id}> byl vyhozen z fronty pro kit **{entry.get('kit')}**."
        )
        await update_panel(interaction.channel, str(entry.get("kit", "")).lower())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Queues(bot))
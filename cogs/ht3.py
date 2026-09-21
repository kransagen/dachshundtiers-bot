"""Cog s HT3+ ticket systému: /sendht3 a /cooldown.

Port původních funkcí:
- /sendht3 – odešle panel s výběrem kitu do určeného kanálu
- HT3+ select menu → kontrola 7denního cooldownu → modál → vytvoření ticket roomky
- Tlačítko Close Ticket → nastavení cooldownu + smazání roomky
- /cooldown – zobrazení cooldownů hráče
"""

import time

import discord
from discord import app_commands
from discord.ext import commands

from config import HT3_COOLDOWN_MS, HT3_PANEL_CHANNEL_ID
from storage import load_data, save_data
from views import HT3PanelView

HT3_PANEL_MESSAGE_FILE = "ht3_panel_message.json"


class HT3(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /sendht3
    # ------------------------------------------------------------------
    @app_commands.command(name="sendht3", description="Pošle panel pro HT3+ tickety")
    async def sendht3(self, interaction: discord.Interaction) -> None:
        target_channel = self.bot.get_channel(HT3_PANEL_CHANNEL_ID)
        if target_channel is None:
            try:
                target_channel = await self.bot.fetch_channel(HT3_PANEL_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target_channel = None

        if target_channel is None:
            return await interaction.response.send_message("Kanál nenalezen!", ephemeral=True)

        embed = (
            discord.Embed(title="💸 Žádost o TierTest", color=0x00FF00)
            .set_description(
                "**Pouze pro HT3+**\n"
                "• Otevírání troll ticketů bude potrestáno!\n"
                "• Po failed tiertestu se dá znova retestovat za 7 dní.\n"
                "• Eval dostanete, když porazíte LT3 testera nebo váš tester "
                "usoudí, že máte HT3 skill.\n"
                "• Bez evalu není možné otevřít HT3+ ticket!"
            )
        )

        view = HT3PanelView()
        message = await target_channel.send(embed=embed, view=view)

        # Uložení zprávy panelu pro re-registraci persistentní view po restartu
        save_data(
            HT3_PANEL_MESSAGE_FILE,
            {"message_id": str(message.id), "channel_id": str(target_channel.id)},
        )
        self.bot.add_view(view, message_id=message.id)

        await interaction.response.send_message(
            "Panel byl úspěšně odeslán!", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /cooldown
    # ------------------------------------------------------------------
    @app_commands.command(name="cooldown", description="Zobrazí tvoje nebo hráčovy cooldowny")
    @app_commands.describe(hrac="Hráč ke kontrole")
    async def cooldown(self, interaction: discord.Interaction, hrac: discord.User = None) -> None:
        target = hrac or interaction.user
        target_id = str(target.id)
        now = time.time() * 1000

        queue_cooldowns = load_data("cooldowns.json", {})
        ht3_cooldowns = load_data("ht3_cooldowns.json", {})
        user_cd = ht3_cooldowns.get(target_id, {})

        text = f"**Cooldowny pro {target.name}**\n\n"

        # Waitlist (queue) cooldown – 4 dny mezi testy
        queue_expiry = queue_cooldowns.get(target_id)
        if queue_expiry and queue_expiry > now:
            remaining = queue_expiry - now
            days = remaining // (24 * 60 * 60 * 1000)
            hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
            minutes = (remaining % (60 * 60 * 1000)) // (60 * 1000)
            text += f"**Waitlist cooldown:** ⏳ Ještě {days}d {hours}h {minutes}m\n\n"
        else:
            text += "**Waitlist cooldown:** žádný\n\n"

        text += "**HT3+ Ticket Cooldowny:**\n"
        has_cooldown = False

        for kit, expire_time in user_cd.items():
            if expire_time > now:
                has_cooldown = True
                remaining = expire_time - now
                days = remaining // (24 * 60 * 60 * 1000)
                hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
                minutes = (remaining % (60 * 60 * 1000)) // (60 * 1000)
                text += f"**{kit}:** ⏳ Ještě {days}d {hours}h {minutes}m\n"

        if not has_cooldown:
            text += "Žádné aktivní HT3+ ticket cooldowny."

        await interaction.response.send_message(content=text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HT3(bot))
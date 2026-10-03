"""Cog se správou registrovaných kitů: /addkit, /removekit, /kits.

Seznam kitů je v PostgreSQL (`kits` – services.kit_catalog) a používají ho:
- HT3+ panel („Žádost o TierTest“ – select menu s kity),
- autocomplete kitu u /createturnaj, /turnajresult a /result.
"""

import discord
from discord import app_commands
from discord.ext import commands

from services.config_store import get_ht3_panel, set_queue_channel_id
from services.kit_catalog import (
    add_kit,
    get_kits,
    set_kit_first_to,
    kit_autocomplete,
    remove_kit,
)
from services.permissions import has_admin_role
from views import HT3PanelView


async def _refresh_ht3_panel(bot) -> bool:
    """Aktualizuje stávající HT3+ panel na nový seznam kitů (pokud existuje)."""
    panel = await get_ht3_panel(
        session_factory=getattr(bot, "db_session_factory", None)
    )
    message_id = panel.get("message_id")
    channel_id = panel.get("channel_id")
    if not message_id or not channel_id:
        return False
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if channel is None:
            return False
        message = await channel.fetch_message(int(message_id))
        kits = await get_kits(
            session_factory=getattr(bot, "db_session_factory", None)
        )
        await message.edit(view=HT3PanelView(kits=kits))
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


class Kits(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /addkit
    # ------------------------------------------------------------------
    @app_commands.command(name="addkit", description="Přidá nový kit do seznamu (HT3+ panel, turnaje)")
    @app_commands.describe(kit="Název nového kitu (např. UHCMace)")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def addkit(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        kit = kit.strip()
        if not kit:
            return await interaction.response.send_message(
                "❌ Zadej platný název kitu.", ephemeral=True
            )

        if not await add_kit(
            kit,
            session_factory=getattr(self.bot, "db_session_factory", None),
        ):
            return await interaction.response.send_message(
                f"❌ Kit **{kit}** už je v seznamu registrovaný.", ephemeral=True
            )

        message = f"✅ Kit **{kit}** byl přidán do seznamu."
        if await _refresh_ht3_panel(self.bot):
            message += "\nHT3+ panel byl automaticky aktualizován."
        else:
            message += "\n💡 Pro aktualizaci HT3+ panelu spusť nové `/sendht3`."

        await interaction.response.send_message(message)

    # ------------------------------------------------------------------
    # /removekit
    # ------------------------------------------------------------------
    @app_commands.command(name="removekit", description="Odebere kit ze seznamu (HT3+ panel, turnaje)")
    @app_commands.describe(kit="Název kitu k odebrání")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def removekit(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        kit = kit.strip()
        if not await remove_kit(
            kit,
            session_factory=getattr(self.bot, "db_session_factory", None),
        ):
            return await interaction.response.send_message(
                f"❌ Kit **{kit}** není v seznamu registrovaný.", ephemeral=True
            )

        message = f"🗑️ Kit **{kit}** byl odebrán ze seznamu."
        if await _refresh_ht3_panel(self.bot):
            message += "\nHT3+ panel byl automaticky aktualizován."
        else:
            message += "\n💡 Pro aktualizaci HT3+ panelu spusť nové `/sendht3`."

        await interaction.response.send_message(message)

    # ------------------------------------------------------------------
    # /setkitft
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setkitft",
        description="Nastaví FT (first to N) HT Fightu pro kit",
    )
    @app_commands.describe(kit="Název kitu", ft="Do kolika vítězných kol se HT Fight hraje")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def setkitft(
        self,
        interaction: discord.Interaction,
        kit: str,
        ft: app_commands.Range[int, 1, 50],
    ) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        name = await set_kit_first_to(
            kit,
            ft,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )
        if name is None:
            return await interaction.response.send_message(
                f"❌ Kit **{kit.strip()}** není v seznamu registrovaný.", ephemeral=True
            )
        await interaction.response.send_message(
            f"✅ Kit **{name}**: HT Fight se hraje na **FT{ft}**.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /addqchannel
    # ------------------------------------------------------------------
    @app_commands.command(
        name="addqchannel",
        description="Nastaví kanál panelu fronty pro kit (kam chodí /openq)",
    )
    @app_commands.describe(
        kit="Název kitu (např. UHCMace)",
        kanal="Kanál pro panel fronty (volitelné; default: tento kanál)",
    )
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def addqchannel(
        self,
        interaction: discord.Interaction,
        kit: str,
        kanal: discord.TextChannel = None,
    ) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        kit_name = kit.strip()
        kit_key = kit_name.lower()
        if not kit_key:
            return await interaction.response.send_message(
                "❌ Zadej platný název kitu.", ephemeral=True
            )

        channel = kanal or interaction.channel
        if channel is None:
            return await interaction.response.send_message(
                "❌ Zadej kanál pro panel (nebo spusť příkaz v textovém kanálu).",
                ephemeral=True,
            )

        registered = await get_kits(
            session_factory=getattr(self.bot, "db_session_factory", None)
        )
        if not any(existing.lower() == kit_key for existing in registered):
            return await interaction.response.send_message(
                f"❌ Kit **{kit_name}** není registrovaný. Nejdřív ho přidej přes `/addkit`.",
                ephemeral=True,
            )

        await set_queue_channel_id(
            kit_key,
            channel.id,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )

        message = f"✅ Panel fronty pro kit **{kit_name}** bude chodit do <#{channel.id}>."
        await interaction.response.send_message(message)

    # ------------------------------------------------------------------
    # /kits
    # ------------------------------------------------------------------
    @app_commands.command(name="kits", description="Vypíše registrované kity")
    async def kits(self, interaction: discord.Interaction) -> None:
        kits = await get_kits(
            session_factory=getattr(self.bot, "db_session_factory", None)
        )
        if not kits:
            return await interaction.response.send_message(
                "Žádné kity nejsou registrované. Přidej je přes `/addkit`.", ephemeral=True
            )

        embed = discord.Embed(
            title="🗂️ Registrované kity",
            description="\n".join(f"• **{kit}**" for kit in kits),
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Kits(bot))
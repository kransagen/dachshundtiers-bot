"""Cog s turnajovým systémem: /createturnaj, /turnajresult, /deleteturnaj.

Port původních funkcí:
- /createturnaj – vytvoření kategorie + přihlašovacího kanálu s tlačítkem
- Přihlašování přes tlačítko a ukončení (automaticky po deadlinu)
- Rozlosování hráčů do skupin a vytvoření skupinových roomek s 1v1 zápasy
- /turnajresult – odeslání výsledku do určeného kanálu
- /deleteturnaj – smazání turnaje i všech kanálů
"""

import asyncio
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import TOP_RESULT_ROLE_ID, TOURNAMENT_RESULT_CHANNEL_ID
from cogs._shared import admin_gate_error
from storage import load_data, save_data
from utils import get_kits, has_tester_role, kit_autocomplete
from views import TournamentSignupView

TOURNAMENT_TIERS = ["LT3", "HT3", "LT2", "HT2", "LT1", "HT1"]


async def end_tournament_signup(guild: discord.Guild, kit_key: str) -> None:
    """Ukončí přihlašování turnaje, zamíchá hráče a vytvoří skupinové roomky."""
    tournaments = load_data("tournaments.json", {})
    tdata = tournaments.get(kit_key)
    if not tdata or tdata.get("ended"):
        return

    tdata["ended"] = True
    save_data("tournaments.json", tournaments)

    signup_channel = guild.get_channel(int(tdata["signupChannelId"]))
    if signup_channel is None:
        try:
            signup_channel = await guild.fetch_channel(int(tdata["signupChannelId"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            signup_channel = None

    category = guild.get_channel(int(tdata["categoryId"]))
    if category is None:
        try:
            category = await guild.fetch_channel(int(tdata["categoryId"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            category = None

    players = tdata.get("participants", [])
    num_groups = max(1, int(tdata.get("groupsCount", 1)))

    if signup_channel is not None:
        try:
            await signup_channel.send(
                content=f"✅ **Přihlašování ukončeno!**\n"
                f"**{len(players)} hráčů** → **{num_groups} skupin**:"
            )
        except discord.HTTPException:
            pass

    if not players:
        return

    # Zamíchání hráčů a rozdělení do skupin
    shuffled = players[:]
    random.shuffle(shuffled)
    groups: list[list[str]] = [[] for _ in range(num_groups)]
    for index, player_id in enumerate(shuffled):
        groups[index % num_groups].append(player_id)

    everyone = guild.default_role

    for group_index, group_players in enumerate(groups, 1):
        if not group_players:
            continue

        # Permice: jen hráči skupiny (+ admini) vidí roomku
        overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for player_id in group_players:
            member = guild.get_member(int(player_id))
            if member is not None:
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                )

        group_channel = await guild.create_text_channel(
            name=f"{kit_key.lower()}-skupina-{group_index}",
            category=category,
            overwrites=overwrites,
        )

        # Zápasy 1v1 (každý zápas na vlastní řádek s koncem řádku jako v originále)
        matches_text = ""
        for j in range(0, len(group_players), 2):
            if j + 1 < len(group_players):
                matches_text += f"<@{group_players[j]}> vs <@{group_players[j + 1]}>\n"
            else:
                matches_text += (
                    f"<@{group_players[j]}> — *čeká na soupeře (lichý počet)*\n"
                )

        players_list = "\n".join(
            f"{i}. <@{p}>" for i, p in enumerate(group_players, 1)
        )
        mentions = " ".join(f"<@{p}>" for p in group_players)

        embed = discord.Embed(
            title=f"🏆 Skupina {group_index} — {tdata['kit']} ({tdata['tier']})",
            description=(
                f"**Tier:** {tdata['tier']}\n"
                f"**Hráči:**\n{players_list}\n\n"
                f"**Zápasy (1v1):**\n{matches_text}\n"
                f"*Tester zapíše výsledky po dokončení zápasů pomocí* `/turnajresult`."
            ),
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )

        try:
            await group_channel.send(content=mentions, embed=embed)
        except discord.HTTPException:
            pass


class Tournaments(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _schedule_end(self, guild_id: int, kit_key: str, seconds: float) -> None:
        """Naplánuje automatické ukončení přihlašování turnaje."""
        async def _task() -> None:
            await asyncio.sleep(max(0.0, seconds))
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(guild_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    guild = None
            if guild is not None:
                await end_tournament_signup(guild, kit_key)

        asyncio.create_task(_task())

    # ------------------------------------------------------------------
    # /createturnaj
    # ------------------------------------------------------------------
    @app_commands.command(name="createturnaj", description="Vytvoří nový turnaj")
    @app_commands.describe(
        role="Role, která dostane přístup",
        skupiny="Počet skupin",
        hodiny="Za kolik hodin skončí přihlašování",
        kit="Vyber kit",
        tier="Vyber cílový tier",
    )
    @app_commands.choices(
        tier=[app_commands.Choice(name=t, value=t) for t in TOURNAMENT_TIERS]
    )
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def createturnaj(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        skupiny: int,
        hodiny: float,
        kit: str,
        tier: str,
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)

        kit_key = kit.lower()
        tournaments = load_data("tournaments.json", {})

        if not get_kits():
            return await interaction.response.send_message(
                "❌ Žádné kity nejsou registrované. Přidej je přes `/addkit`.",
                ephemeral=True,
            )

        if kit_key in tournaments:
            return await interaction.response.send_message(
                f"❌ Turnaj pro kit **{kit}** už právě probíhá! Nemůžeš vytvořit další "
                f"dokud nepoužiješ `/deleteturnaj`.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        everyone = interaction.guild.default_role
        category = await interaction.guild.create_category(
            name=f"🏆 {kit} {tier} Turnaj",
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True),
            },
        )

        signup_channel = await interaction.guild.create_text_channel(
            name=f"{kit_key}-{tier.lower()}-turnaj-a-sign-up",
            category=category,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            },
        )

        deadline_ms = time.time() * 1000 + hodiny * 3600 * 1000
        unix_time = int(deadline_ms / 1000)

        embed = discord.Embed(
            title=f"🏆 {kit.upper()} TURNAJ — {tier}",
            description=(
                "Klikni na tlačítko ✅ pro přihlášení do turnaje!\n\n"
                f"**Kit:** {kit}\n"
                f"**Tier:** {tier}\n"
                f"**Skupin:** {skupiny}\n"
                f"**Deadline:** <t:{unix_time}:F> (<t:{unix_time}:R>)"
            ),
            color=0xF1C40F,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Vytvořil: {interaction.user.name}")

        view = TournamentSignupView(kit_key)
        message = await signup_channel.send(
            content=f"{role.mention}", embed=embed, view=view
        )

        tournaments[kit_key] = {
            "kit": kit,
            "tier": tier,
            "groupsCount": skupiny,
            "categoryId": str(category.id),
            "signupChannelId": str(signup_channel.id),
            "signupMessageId": str(message.id),
            "roleId": str(role.id),
            "guildId": str(interaction.guild.id),
            "participants": [],
            "ended": False,
            "deadline": int(deadline_ms),
        }
        save_data("tournaments.json", tournaments)

        self.bot.add_view(view, message_id=message.id)
        self._schedule_end(interaction.guild.id, kit_key, hodiny * 3600)

        await interaction.followup.send(
            f"Turnaj **{kit} {tier}** byl úspěšně vytvořen v kanále <#{signup_channel.id}>!",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /turnajresult
    # ------------------------------------------------------------------
    @app_commands.command(name="turnajresult", description="Zapiš výsledek turnaje")
    @app_commands.describe(
        kit="Kit turnaje",
        hrac="Hráč, který dostává promote",
        z_tieru="Současný tier hráče",
        na_tier="Nový tier hráče",
    )
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def turnajresult(
        self,
        interaction: discord.Interaction,
        kit: str,
        hrac: discord.User,
        z_tieru: str,
        na_tier: str,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro testery.", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        target_channel = interaction.guild.get_channel(TOURNAMENT_RESULT_CHANNEL_ID)
        if target_channel is None:
            try:
                target_channel = await interaction.guild.fetch_channel(
                    TOURNAMENT_RESULT_CHANNEL_ID
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target_channel = None

        if target_channel is None:
            return await interaction.response.send_message(
                f"❌ Kanál pro výsledky s ID `{TOURNAMENT_RESULT_CHANNEL_ID}` nebyl nalezen!",
                ephemeral=True,
            )

        # Ping jde POUZE na nakonfigurovanou TOP_RESULT_ROLE_ID (jako /topresult),
        # nikdy na @everyone – role se nebere od uživatele.
        target_role = (
            interaction.guild.get_role(TOP_RESULT_ROLE_ID)
            if interaction.guild is not None
            else None
        )
        if target_role is None:
            return await interaction.response.send_message(
                f"❌ Role pro turnajový ping (ID `{TOP_RESULT_ROLE_ID}`) nebyla "
                "na serveru nalezena – zkontroluj `TOP_RESULT_ROLE_ID`.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"🏆 {kit.upper()} TURNAJ",
            description=f"**Získává:**\n> <@{hrac.id}> — {z_tieru} ➔ **{na_tier}**",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Zapisovatel: {interaction.user.name}")

        allowed = discord.AllowedMentions(everyone=False, users=True, roles=[target_role])
        await target_channel.send(
            content=f"<@&{TOP_RESULT_ROLE_ID}>", embed=embed, allowed_mentions=allowed
        )
        await interaction.response.send_message(
            f"Výsledek byl úspěšně odeslán do <#{TOURNAMENT_RESULT_CHANNEL_ID}>.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /deleteturnaj
    # ------------------------------------------------------------------
    @app_commands.command(name="deleteturnaj", description="Smaže turnaj a všechny jeho kanály")
    @app_commands.describe(kit="Kit turnaje ke smazání")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def deleteturnaj(self, interaction: discord.Interaction, kit: str) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)

        kit_key = kit.lower()
        tournaments = load_data("tournaments.json", {})

        if kit_key not in tournaments:
            return await interaction.response.send_message(
                f"Žádný aktivní turnaj pro kit **{kit}** neexistuje.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        category_id = int(tournaments[kit_key]["categoryId"])
        category = interaction.guild.get_channel(category_id)
        if category is None:
            try:
                category = await interaction.guild.fetch_channel(category_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                category = None

        if category is not None:
            # Smazání všech kanálů v kategorii
            for channel in category.channels:
                try:
                    await channel.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            try:
                await category.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        del tournaments[kit_key]
        save_data("tournaments.json", tournaments)

        await interaction.followup.send(
            f"Turnaj pro kit **{kit}** a všechny jeho kanály byly kompletně smazány.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tournaments(bot))
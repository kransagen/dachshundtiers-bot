"""Cog s turnajovým systémem: /createturnaj, /turnajresult, /deleteturnaj.

Port původních funkcí:
- /createturnaj – vytvoření kategorie + přihlašovacího kanálu s tlačítkem
- Přihlašování přes tlačítko a ukončení (automaticky po deadlinu)
- Rozlosování hráčů do skupin a vytvoření skupinových roomek s 1v1 zápasy
- /turnajresult – odeslání výsledku do určeného kanálu
- /deleteturnaj – smazání turnaje i všech kanálů

Fáze F (F2/F3): stav turnaje žije v PostgreSQL (``tournaments`` +
``tournament_entries`` přes TournamentRepository) místo tournaments.json.
Bez dostupné DB se operace zastaví s jasnou chybou – žádný JSON fallback.
"""

import asyncio
import random
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config import TOP_RESULT_ROLE_ID, TOURNAMENT_RESULT_CHANNEL_ID
from cogs._shared import admin_gate_error
from db.repositories.kits import KitRepository
from db.repositories.tournaments import TournamentRepository
from db.services.session import transaction
from services.kit_catalog import get_kits
from utils import has_tester_role, kit_autocomplete
from views import TournamentSignupView

TOURNAMENT_TIERS = ["LT3", "HT3", "LT2", "HT2", "LT1", "HT1"]


async def end_tournament_signup(
    session_factory, guild: discord.Guild, kit_key: str
) -> None:
    """Ukončí přihlašování turnaje, zamíchá hráče a vytvoří skupinové roomky."""
    if session_factory is None:
        return
    async with transaction(session_factory) as session:
        kit = await KitRepository().get_by_key(session, kit_key)
        if kit is None:
            return
        tournament = await TournamentRepository().get_by_kit(session, kit_id=kit.id)
        if tournament is None or tournament.ended:
            return
        kit_name = kit.name
        tier = tournament.tier
        num_groups = max(1, tournament.groups_count)
        signup_channel_id = tournament.signup_channel_id
        category_id = tournament.category_id
        players = await TournamentRepository().list_participant_ids(
            session, tournament.id
        )
        await TournamentRepository().mark_ended(session, tournament.id)

    signup_channel = guild.get_channel(signup_channel_id)
    if signup_channel is None:
        try:
            signup_channel = await guild.fetch_channel(signup_channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            signup_channel = None

    category = guild.get_channel(category_id)
    if category is None:
        try:
            category = await guild.fetch_channel(category_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            category = None

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
    groups: list[list[int]] = [[] for _ in range(num_groups)]
    for index, player_id in enumerate(shuffled):
        groups[index % num_groups].append(player_id)

    everyone = guild.default_role

    for group_index, group_players in enumerate(groups, 1):
        if not group_players:
            continue

        # Permice: jen hráči skupiny (+ admini) vidí roomku
        overwrites = {everyone: discord.PermissionOverwrite(view_channel=False)}
        for player_id in group_players:
            member = guild.get_member(player_id)
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
            title=f"🏆 Skupina {group_index} — {kit_name} ({tier})",
            description=(
                f"**Tier:** {tier}\n"
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
                await end_tournament_signup(
                    getattr(self.bot, "db_session_factory", None), guild, kit_key
                )

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

        # Katalog z PostgreSQL; prázdný = opravdu nejsou registrované žádné
        # kity (kit_catalog navíc vrací DEFAULT_KITS, když je DB úplně prázdná,
        # takže tento check znamená "ani jeden nebyl nikdy přidán").
        if not await get_kits(
            session_factory=getattr(self.bot, "db_session_factory", None)
        ):
            return await interaction.response.send_message(
                "❌ Žádné kity nejsou registrované. Přidej je přes `/addkit`.",
                ephemeral=True,
            )

        session_factory = getattr(self.bot, "db_session_factory", None)
        if session_factory is None:
            return await interaction.response.send_message(
                "❌ PostgreSQL není dostupné — nelze vytvořit turnaj.",
                ephemeral=True,
            )

        async with transaction(session_factory) as session:
            kit_row = await KitRepository().get_by_key(session, kit_key)
            if kit_row is None:
                return await interaction.response.send_message(
                    f"❌ Kit **{kit}** není v databázi zaregistrovaný.",
                    ephemeral=True,
                )
            if (
                await TournamentRepository().get_by_kit(session, kit_id=kit_row.id)
                is not None
            ):
                return await interaction.response.send_message(
                    f"❌ Turnaj pro kit **{kit}** už právě probíhá! Nemůžeš vytvořit další "
                    f"dokud nepoužiješ `/deleteturnaj`.",
                    ephemeral=True,
                )
            kit_name = kit_row.name

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

        async with transaction(session_factory) as session:
            await TournamentRepository().create(
                session,
                kit_id=kit_row.id,
                name=kit_name,
                tier=tier,
                groups_count=skupiny,
                category_id=category.id,
                signup_channel_id=signup_channel.id,
                signup_message_id=message.id,
                role_id=role.id,
                guild_id=interaction.guild.id,
                deadline=datetime.fromtimestamp(
                    deadline_ms / 1000, tz=timezone.utc
                ),
            )

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
        session_factory = getattr(self.bot, "db_session_factory", None)
        if session_factory is None:
            return await interaction.response.send_message(
                "❌ PostgreSQL není dostupné — nelze smazat turnaj.",
                ephemeral=True,
            )

        async with transaction(session_factory) as session:
            kit_row = await KitRepository().get_by_key(session, kit_key)
            if kit_row is None:
                return await interaction.response.send_message(
                    f"Žádný aktivní turnaj pro kit **{kit}** neexistuje.",
                    ephemeral=True,
                )
            tournament = await TournamentRepository().get_by_kit(
                session, kit_id=kit_row.id
            )
            if tournament is None:
                return await interaction.response.send_message(
                    f"Žádný aktivní turnaj pro kit **{kit}** neexistuje.",
                    ephemeral=True,
                )
            category_id = tournament.category_id
            tournament_id = tournament.id

        await interaction.response.defer(ephemeral=True)

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

        async with transaction(session_factory) as session:
            await TournamentRepository().delete(session, tournament_id)

        await interaction.followup.send(
            f"Turnaj pro kit **{kit}** a všechny jeho kanály byly kompletně smazány.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tournaments(bot))
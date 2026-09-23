"""Cog s HT3+ ticket systémem: panel, vytvoření ticketu a správa.

Port původních funkcí + Phase 2 (HT evaluation tickets):
- /sendht3 – odešle panel s výběrem kitu do určeného kanálu
- HT3+ select menu → kontrola 7denního cooldownu → modál → vytvoření ticketu
  (automatické vytvoření s prevencí duplicit, vlastnictvím a restart-safe stavem)
- Tlačítka ticketu Claim HT / Unclaim / Close / Reopen (persistentní view)
- /add   – přidá hráče do ticketu (přístup + záznam),
- /remove– odebere hráče z ticketu (zruší přístup),
- /claim / /unclaim – převzetí / vzdání se ticketu testerem,
- /cooldown – zobrazení cooldownů hráče,
- /seteval / /uneval – správa „LT3 + eval".

Log událostí ticketů (created / claimed / unclaimed / added / removed /
closed / reopened) je v ``data/ht_ticket_logs.json`` – restart-safe.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import HT3_PANEL_CHANNEL_ID
from services.tickets import (
    add_member,
    claim_ticket,
    get_ticket,
    log_ticket_event,
    remove_member,
    unclaim_ticket,
)
from storage import load_data, save_data
from utils import has_tester_role, kit_autocomplete, set_eval, unset_eval
from views import (
    HT3PanelView,
    grant_channel_access,
    revoke_channel_access,
    ticket_embed,
)

log = logging.getLogger("dachshundtiers")

HT3_PANEL_MESSAGE_FILE = "ht3_panel_message.json"


async def _sync_ticket_embed(bot, ticket: dict) -> None:
    """Aktualizuje embed panel zprávy ticketu po změně stavu (best effort)."""
    if not isinstance(ticket, dict):
        return
    ch_id = ticket.get("id")
    msg_id = ticket.get("panelMessageId")
    if not ch_id or not msg_id:
        return
    try:
        channel = bot.get_channel(int(ch_id))
        if channel is None:
            channel = await bot.fetch_channel(int(ch_id))
        if channel is None:
            return
        message = await channel.fetch_message(int(msg_id))
        await message.edit(embed=ticket_embed(ticket))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        pass


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

        embed = discord.Embed(
            title="💸 Žádost o TierTest",
            description=(
                "**Pouze pro HT3+**\n"
                "• Otevírání troll ticketů bude potrestáno!\n"
                "• Po failed tiertestu se dá znova retestovat za 7 dní.\n"
                "• Eval dostanete, když porazíte LT3 testera nebo váš tester "
                "usoudí, že máte HT3 skill.\n"
                "• LT3 + eval = status mezi LT3 a HT3 – stejná role, ale "
                "můžete otevírat HT3+ tickety.\n"
                "• Bez evalu není možné otevřít HT3+ ticket!"
            ),
            color=0x00FF00,
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
    # /add – přidá hráče do aktuálního HT ticketu / tester roomky
    # ------------------------------------------------------------------
    @app_commands.command(
        name="add",
        description="Přidá hráče do aktuálního HT ticketu (přístup + sledování)",
    )
    @app_commands.describe(hrac="Hráč, kterého přidat do ticketu")
    async def add(self, interaction: discord.Interaction, hrac: discord.Member) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ /add funguje jen v textovém kanálu (ticketu).", ephemeral=True
            )

        ticket = await get_ticket(channel.id)

        # HT ticket (Phase 2): přidání do stavu ticketu + log + embed
        if ticket is not None:
            if ticket.get("status") != "open":
                return await interaction.response.send_message(
                    "❌ Ticket je zavřený – přidávat hráče jde jen do otevřeného.",
                    ephemeral=True,
                )
            result = await add_member(channel.id, str(hrac.id))
            r = result["result"]
            if r == "is_owner":
                return await interaction.response.send_message(
                    "❌ Vlastník ticketu už přístup má.", ephemeral=True
                )
            if r == "already_member":
                await _ticket_set_perms(channel, hrac)
                await interaction.response.send_message(
                    f"ℹ️ Hráč **{hrac.display_name}** (<@{hrac.id}>) už v ticketu "
                    f"je – přístup byl znovu potvrzen.",
                    ephemeral=True,
                )
                return
            if r != "added":
                return await interaction.response.send_message(
                    "❌ Hráče se nepodařilo přidat.", ephemeral=True
                )

            await _ticket_set_perms(channel, hrac)
            await log_ticket_event(
                channel.id,
                "added",
                str(interaction.user.id),
                interaction.user.display_name,
                details=f"Přidán hráč <@{hrac.id}> ({hrac.display_name})",
            )
            await _sync_ticket_embed(self.bot, result["ticket"])
            return await interaction.response.send_message(
                f"✅ Hráč **{hrac.display_name}** (<@{hrac.id}>) byl přidán do "
                f"ticketu <#{channel.id}>. Po `/result` mu bude přístup odebrán."
            )

        # Tester roomka (legacy): přímá práva + záznam pro /result
        try:
            await channel.set_permissions(hrac, view_channel=True, send_messages=True)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                "❌ Bot nemůže měnit práva kanálu – zkontroluj oprávnění "
                "(Manage Channels).",
                ephemeral=True,
            )

        # Záznam, aby /result hráči práva po testu odebral i v tomhle kanálu
        pulled = load_data("pulled_players.json", {})
        pulled[str(hrac.id)] = {
            "channel": str(channel.id),
            "player": {
                "id": str(hrac.id),
                "username": hrac.display_name,
                "ign": hrac.display_name,
                "kit": "",
                "joinedAt": 0,
            },
        }
        save_data("pulled_players.json", pulled)

        await interaction.response.send_message(
            f"✅ Hráč **{hrac.display_name}** (<@{hrac.id}>) byl přidán do "
            f"ticketu <#{channel.id}>. Po `/result` mu bude přístup odebrán."
        )

    # ------------------------------------------------------------------
    # /remove – odebere hráče z aktuálního HT ticketu
    # ------------------------------------------------------------------
    @app_commands.command(
        name="remove",
        description="Odebere hráče z aktuálního HT ticketu (zruší přístup)",
    )
    @app_commands.describe(hrac="Hráč, kterého odebrat z ticketu")
    async def remove(self, interaction: discord.Interaction, hrac: discord.Member) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ /remove funguje jen v textovém kanálu (ticketu).", ephemeral=True
            )

        ticket = await get_ticket(channel.id)
        if ticket is None:
            return await interaction.response.send_message(
                "❌ Tento kanál není HT ticket – /remove funguje jen v ticketu.",
                ephemeral=True,
            )

        result = await remove_member(channel.id, str(hrac.id))
        r = result["result"]
        if r == "is_owner":
            return await interaction.response.send_message(
                "❌ Vlastníka ticketu nejde odebrat.", ephemeral=True
            )
        if r == "is_claimer":
            return await interaction.response.send_message(
                "❌ Tester s převzatým ticketem nejde odebrat – nejdřív použij "
                "`/unclaim` (nebo tlačítko Unclaim).",
                ephemeral=True,
            )
        if r == "not_member":
            return await interaction.response.send_message(
                f"ℹ️ Hráč **{hrac.display_name}** (<@{hrac.id}>) v ticketu nebyl "
                f"– žádný přístup k odebrání.",
                ephemeral=True,
            )
        if r != "removed":
            return await interaction.response.send_message(
                "❌ Hráče se nepodařilo odebrat.", ephemeral=True
            )

        await revoke_channel_access(channel, str(hrac.id))
        await log_ticket_event(
            channel.id,
            "removed",
            str(interaction.user.id),
            interaction.user.display_name,
            details=f"Odebrán hráč <@{hrac.id}> ({hrac.display_name})",
        )
        await _sync_ticket_embed(self.bot, result["ticket"])
        await interaction.response.send_message(
            f"✅ Hráč **{hrac.display_name}** (<@{hrac.id}>) byl odebrán z "
            f"ticketu <#{channel.id}> a ztratil přístup."
        )

    # ------------------------------------------------------------------
    # /claim – tester si převezme HT ticket
    # ------------------------------------------------------------------
    @app_commands.command(
        name="claim",
        description="Převezme si aktuální HT ticket (Claim HT)",
    )
    async def claim(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ /claim funguje jen v textovém kanálu (ticketu).", ephemeral=True
            )

        ticket = await get_ticket(channel.id)
        if ticket is None:
            return await interaction.response.send_message(
                "❌ Tento kanál není HT ticket – /claim funguje jen v ticketu.",
                ephemeral=True,
            )
        if ticket.get("status") != "open":
            return await interaction.response.send_message(
                "❌ Ticket je zavřený – nejdřív ho otevři (Reopen).", ephemeral=True
            )

        result = await claim_ticket(
            channel.id, str(interaction.user.id), interaction.user.display_name
        )
        r = result["result"]
        if r == "own_ticket":
            return await interaction.response.send_message(
                "❌ Nemůžeš si převzít vlastní ticket.", ephemeral=True
            )
        if r == "already_claimed":
            other = result.get("claimer_name") or f"<@{result.get('claimer_id')}>"
            return await interaction.response.send_message(
                f"❌ Ticket už má převzatý **{other}** – nejdřív se ho musí vzdát.",
                ephemeral=True,
            )
        if r != "claimed":
            return await interaction.response.send_message(
                "❌ Ticket se nepodařilo převzít.", ephemeral=True
            )

        await grant_channel_access(channel, str(interaction.user.id))
        await log_ticket_event(
            channel.id,
            "claimed",
            str(interaction.user.id),
            interaction.user.display_name,
            details=f"Claim: {result['ticket'].get('ign')} / {result['ticket'].get('kit')}",
        )
        await _sync_ticket_embed(self.bot, result["ticket"])
        await interaction.response.send_message(
            f"✅ **{interaction.user.display_name}** převzal/a ticket "
            f"<#{channel.id}> – můžeš začít test.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /unclaim – tester se vzdá HT ticketu
    # ------------------------------------------------------------------
    @app_commands.command(
        name="unclaim",
        description="Vzdá se aktuálního HT ticketu (uvolní ho)",
    )
    async def unclaim(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "❌ /unclaim funguje jen v textovém kanálu (ticketu).", ephemeral=True
            )

        ticket = await get_ticket(channel.id)
        if ticket is None:
            return await interaction.response.send_message(
                "❌ Tento kanál není HT ticket – /unclaim funguje jen v ticketu.",
                ephemeral=True,
            )

        result = await unclaim_ticket(channel.id, str(interaction.user.id), force=True)
        if result["result"] != "unclaimed":
            return await interaction.response.send_message(
                "❌ Ticket nemá nikdo převzatý (nebo se nepodařilo uvolnit).",
                ephemeral=True,
            )

        previous = result["previous"]
        await revoke_channel_access(channel, previous["claimer_id"])
        await log_ticket_event(
            channel.id,
            "unclaimed",
            str(interaction.user.id),
            interaction.user.display_name,
            details=f"Vzdal se: {previous['claimer_name'] or previous['claimer_id']}",
        )
        await _sync_ticket_embed(self.bot, result["ticket"])
        await interaction.response.send_message(
            "↩️ Ticket je zase volný – nikdo ho nemá převzatý.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /seteval / /uneval – status „LT3 + eval" hráči
    # ------------------------------------------------------------------
    @app_commands.command(
        name="seteval",
        description="Nastaví hráči „LT3 + eval“ pro kit (může otevírat HT3+ tickety)",
    )
    @app_commands.describe(ign="Minecraft IGN hráče", kit="Kit")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def seteval(self, interaction: discord.Interaction, ign: str, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )

        if set_eval(ign, kit):
            await interaction.response.send_message(
                f"✅ **{ign.strip()}** dostal „LT3 + eval“ pro kit **{kit.strip()}** – "
                f"může otevírat HT3+ tickety (role zůstává LT3)."
            )
        else:
            await interaction.response.send_message(
                f"❌ Neplatný IGN nebo kit (`{ign}` / `{kit}`).", ephemeral=True
            )

    @app_commands.command(
        name="uneval",
        description="Odebere hráči „LT3 + eval“ pro kit (nemůže otevírat HT3+ tickety)",
    )
    @app_commands.describe(ign="Minecraft IGN hráče", kit="Kit")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def uneval(self, interaction: discord.Interaction, ign: str, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )

        if unset_eval(ign, kit):
            await interaction.response.send_message(
                f"⛔ **{ign.strip()}** přišel o „LT3 + eval“ pro kit **{kit.strip()}** – "
                f"HT3+ tickety už otevírat nemůže."
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ **{ign.strip()}** nemá eval pro kit **{kit.strip()}** – nic se neměnilo.",
                ephemeral=True,
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


async def _ticket_set_perms(channel, member: discord.Member) -> None:
    """Nastaví hráči přístup do kanálu ticketu (best effort, zaloguje chybu)."""
    try:
        await channel.set_permissions(member, view_channel=True, send_messages=True)
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze udělit přístup do ticketu %s pro %s: %s", channel.id, member.id, err)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HT3(bot))
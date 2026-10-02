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
closed / reopened) je v PostgreSQL ``ticket_members`` / ``audit_logs`` –
restart-safe.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import HT3_PANEL_CHANNEL_ID
from services.config_store import set_ht3_panel
from services.cooldowns import get_cooldowns
from services.evals import set_eval, unset_eval
from services.ht3_tickets import ensure_eval_ticket
from services.kit_catalog import get_kits
from services.permissions import has_admin_role
from services.queue_service import is_tester_room, preset_player_room
from services.tickets import (
    add_member,
    claim_ticket,
    get_ticket,
    remove_member,
    unclaim_ticket,
)
from services.permissions import has_admin_role
from utils import has_tester_role, kit_autocomplete
from views import (
    HT3PanelView,
    _open_ht3_ticket,
    grant_channel_access,
    revoke_channel_access,
    ticket_embed,
)

log = logging.getLogger("dachshundtiers")


def _eval_ticket_skip_note(request) -> str:
    """Vysvětlení, proč se při /seteval ticket nezaložil.

    Každý důvod je záměrný, ne chyba, a hráč ho potřebuje vidět – jinak by
    „eval má, ale ticket není" vypadalo jako chyba bota.
    """
    reason = request.reason
    if reason == "already_open":
        ch = (request.open_ticket or {}).get("id")
        return (
            "ℹ️ Už má otevřený HT3+ ticket pro tenhle kit"
            + (f": <#{ch}>." if ch else ".")
            + " Nový se nevytváří."
        )
    if reason == "no_tier":
        return (
            "ℹ️ HT3+ ticket se nezaložil: hráč nemá u tohoto kitu uložený žádný "
            "tier, takže se nedá odvodit, na co ho poslat. Nejdřív `/result`."
        )
    if reason == "no_player":
        return "ℹ️ Hráč není v databázi, ticket se nezaložil."
    if reason == "no_kit":
        return "ℹ️ Kit neznámý, ticket se nezaložil."
    if reason == "no_discord":
        return (
            "ℹ️ Hráč nemá propojený Discord účet, ticket se nezaložil. "
            "Otevře si ho sám přes HT3+ panel."
        )
    if reason == "cooldown":
        remaining = int(request.remaining_ms or 0)
        days, rem = divmod(remaining, 86_400_000)
        hours = rem // 3_600_000
        return (
            f"ℹ️ Hráč má HT3+ cooldown na tenhle kit ještě **{days}d {hours}h**, "
            "ticket se nezaložil. Eval zůstává, ticket si otevře po skončení cooldownu."
        )
    if reason == "no_database":
        return "ℹ️ Bez PostgreSQL evalu nejsou uložené trvalé, ticket se nezaložil."
    return "ℹ️ HT3+ ticket se nezaložil."


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
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Admin!", ephemeral=True
            )

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

        kits = await get_kits(
            session_factory=getattr(self.bot, "db_session_factory", None)
        )
        view = HT3PanelView(kits=kits)
        message = await target_channel.send(embed=embed, view=view)

        # Uložení zprávy panelu pro re-registraci persistentní view po restartu
        await set_ht3_panel(
            message.id,
            target_channel.id,
            session_factory=getattr(self.bot, "db_session_factory", None),
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

        ticket = await get_ticket(channel.id, session_factory=getattr(self.bot, "db_session_factory", None))

        # HT ticket (Phase 2): přidání do stavu ticketu + log + embed
        if ticket is not None:
            if ticket.get("status") != "open":
                return await interaction.response.send_message(
                    "❌ Ticket je zavřený – přidávat hráče jde jen do otevřeného.",
                    ephemeral=True,
                )
            result = await add_member(
                channel.id,
                str(hrac.id),
                session_factory=getattr(self.bot, "db_session_factory", None),
                audit={
                    "actor_id": str(interaction.user.id),
                    "actor_name": interaction.user.display_name,
                    "details": f"Přidán hráč <@{hrac.id}> ({hrac.display_name})",
                },
            )
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
            await _sync_ticket_embed(self.bot, result["ticket"])
            return await interaction.response.send_message(
                f"✅ Hráč **{hrac.display_name}** (<@{hrac.id}>) byl přidán do "
                f"ticketu <#{channel.id}>. Po `/result` mu bude přístup odebrán."
            )

        # Tester roomka: přímá práva + záznam pro /result. Jen v roomce
        # zaregistrované přes /mktesterroom, ne v libovolném kanálu.
        if not await is_tester_room(
            channel.id, session_factory=getattr(self.bot, "db_session_factory", None)
        ):
            return await interaction.response.send_message(
                "❌ /add funguje jen v HT ticketu nebo tester roomce.", ephemeral=True
            )
        try:
            await channel.set_permissions(hrac, view_channel=True, send_messages=True)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                "❌ Bot nemůže měnit práva kanálu – zkontroluj oprávnění "
                "(Manage Channels).",
                ephemeral=True,
            )

        # Záznam, aby /result hráči práva po testu odebral i v tomhle kanálu
        await preset_player_room(
            {
                "id": str(hrac.id),
                "username": hrac.display_name,
                "ign": hrac.display_name,
                "kit": "",
                "joinedAt": 0,
            },
            channel.id,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )

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

        ticket = await get_ticket(channel.id, session_factory=getattr(self.bot, "db_session_factory", None))
        if ticket is None:
            return await interaction.response.send_message(
                "❌ Tento kanál není HT ticket – /remove funguje jen v ticketu.",
                ephemeral=True,
            )

        result = await remove_member(
            channel.id,
            str(hrac.id),
            session_factory=getattr(self.bot, "db_session_factory", None),
            audit={
                "actor_id": str(interaction.user.id),
                "actor_name": interaction.user.display_name,
                "details": f"Odebrán hráč <@{hrac.id}> ({hrac.display_name})",
            },
        )
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

        ticket = await get_ticket(channel.id, session_factory=getattr(self.bot, "db_session_factory", None))
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
            channel.id,
            str(interaction.user.id),
            interaction.user.display_name,
            session_factory=getattr(self.bot, "db_session_factory", None),
            audit={
                "actor_id": str(interaction.user.id),
                "actor_name": interaction.user.display_name,
                "details": f"Claim: {ticket.get('ign')} / {ticket.get('kit')}",
            },
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

        ticket = await get_ticket(channel.id, session_factory=getattr(self.bot, "db_session_factory", None))
        if ticket is None:
            return await interaction.response.send_message(
                "❌ Tento kanál není HT ticket – /unclaim funguje jen v ticketu.",
                ephemeral=True,
            )

        result = await unclaim_ticket(
            channel.id,
            str(interaction.user.id),
            force=has_admin_role(interaction.user),
            session_factory=getattr(self.bot, "db_session_factory", None),
            audit={
                "actor_id": str(interaction.user.id),
                "actor_name": interaction.user.display_name,
            },
        )
        if result["result"] == "not_claimer":
            other = result.get("claimer_name") or f"<@{result.get('claimer_id')}>"
            return await interaction.response.send_message(
                f"❌ Ticket má převzatý **{other}** – uvolnit ho může jen on nebo admin.",
                ephemeral=True,
            )
        if result["result"] != "unclaimed":
            return await interaction.response.send_message(
                "❌ Ticket nemá nikdo převzatý (nebo se nepodařilo uvolnit).",
                ephemeral=True,
            )

        previous = result["previous"]
        await revoke_channel_access(channel, previous["claimer_id"])
        await _sync_ticket_embed(self.bot, result["ticket"])
        await interaction.response.send_message(
            "↩️ Ticket je zase volný – nikdo ho nemá převzatý.", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /seteval / /uneval – status „LT3 + eval" hráči
    # ------------------------------------------------------------------
    @app_commands.command(
        name="seteval",
        description="Nastaví hráči „LT3 + eval“ pro kit a založí HT3+ ticket",
    )
    @app_commands.describe(ign="Minecraft IGN hráče", kit="Kit")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def seteval(self, interaction: discord.Interaction, ign: str, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )

        session_factory = getattr(self.bot, "db_session_factory", None)
        if not await set_eval(
            ign,
            kit,
            granted_by=interaction.user.id,
            session_factory=session_factory,
        ):
            return await interaction.response.send_message(
                f"❌ Neplatný IGN nebo kit (`{ign}` / `{kit}`).", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Eval rovnou otevírá HT3+ ticket: hráč má evalu, takže ticket pro
        # tenhle kit je povolený a není důvod nechat ho hledat v panelu.
        # Rozhodnutí je v DB-sloužbě a je IDEMPOTNÍ – znovu spuštěné /seteval
        # (třeba po timeoutu) druhý ticket nezaloží, jen odkáže na existující.
        request = await ensure_eval_ticket(ign, kit, session_factory=session_factory)

        summary = (
            f"✅ **{ign.strip()}** dostal „LT3 + eval“ pro kit "
            f"**{kit.strip()}** (role zůstává LT3)."
        )

        if not request.needs_ticket:
            summary += "\n" + _eval_ticket_skip_note(request)
            return await interaction.followup.send(summary, ephemeral=True)

        opened = await _open_ht3_ticket(
            interaction,
            ign=request.ign,
            kit=request.kit,
            target_tier=request.target_tier,
            current_tier=request.current_tier,
            eval_ok=True,
            owner_id=str(request.discord_id),
            session_factory=session_factory,
        )
        if opened.channel_id is None:
            return await interaction.followup.send(
                f"{summary}\n⚠️ Ticket se nepodařilo založit: "
                f"{opened.message or 'neznámá chyba'}",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"{summary}\n🎫 HT3+ ticket založen: <#{opened.channel_id}> "
            f"({request.target_tier} / {request.kit})",
            ephemeral=True,
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

        if await unset_eval(
            ign,
            kit,
            session_factory=getattr(self.bot, "db_session_factory", None),
        ):
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

        cooldowns = await get_cooldowns(
            target_id,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )
        waitlist_cd = cooldowns["waitlist"]
        legacy_global_ms = cooldowns["waitlist_legacy_global_ms"]
        user_cd = cooldowns["ht3"]

        def _fmt(remaining: int) -> str:
            days = remaining // (24 * 60 * 60 * 1000)
            hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
            minutes = (remaining % (60 * 60 * 1000)) // (60 * 1000)
            return f"⏳ Ještě {days}d {hours}h {minutes}m"

        text = f"**Cooldowny pro {target.name}**\n\n"

        text += "**Waitlist cooldowny:**\n"
        if waitlist_cd:
            for kit, remaining in waitlist_cd.items():
                text += f"**{kit}:** {_fmt(remaining)}\n"
        if legacy_global_ms is not None:
            text += f"**(starý obecný cooldown, blokuje všechny kity):** {_fmt(legacy_global_ms)}\n"
        if not waitlist_cd and legacy_global_ms is None:
            text += "žádný\n"
        text += "\n"

        text += "**HT3+ Ticket Cooldowny:**\n"
        has_cooldown = False

        for kit, remaining in user_cd.items():
            has_cooldown = True
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
"""Interaktivní komponenty: tlačítka, select menu a modály.

Odpovídají původním discord.js interakcím (joinbtn / leavebtn / pullbtn,
joinmodal, ht3_select_kit, ht3_modal, close_ht3, signup_turnaj).
"""

import asyncio
import logging
import time

import discord

from config import HT3_COOLDOWN_MS, PLAYER_COOLDOWN_MS, TIERS_UPPER, get_ht3_ticket_category
from panel import update_panel
from services.permissions import get_tester_roles
from services.queue_service import (
    join_queue,
    leave_queue,
    pop_for_kit,
    remove_by_player_id,
    save_pulled_player,
)
from services.tickets import (
    HT3_TIER_LADDER,
    claim_ticket,
    close_ticket,
    create_ticket,
    effective_ticket_tier,
    find_open_ticket,
    find_player_tier,
    get_ticket,
    log_ticket_event,
    next_ticket_tier,
    reopen_ticket,
    set_panel_message,
    tier_allows_tickets,
    unclaim_ticket,
)
from storage import load_data, save_data
from utils import DEFAULT_KITS, get_kits, has_eval, has_tester_role

log = logging.getLogger("dachshundtiers")


# ---------------------------------------------------------------------------
# Bezpečné View / Modal: neošetřené chyby se zalogují a hráč dostane hlášku
# ---------------------------------------------------------------------------
class SafeView(discord.ui.View):
    """View, který neošetřené chyby callbacků zaloguje a pošle hráči hlášku."""

    async def on_error(self, interaction, error, item):
        log.exception("Chyba v komponentě %s: %s", item, error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden):
            pass


class SafeModal(discord.ui.Modal):
    """Modál, který neošetřené chyby zaloguje a pošle hráči hlášku."""

    async def on_error(self, interaction, error):
        log.exception("Chyba v modálu: %s", error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden):
            pass


# ---------------------------------------------------------------------------
# Modál pro zadání IGN při připojení do fronty (joinbtn → joinmodal)
# ---------------------------------------------------------------------------
class JoinModal(SafeModal):
    def __init__(self, kit: str):
        super().__init__(title=f"Join {kit} Queue")
        self.kit = kit
        self.ign_input = discord.ui.TextInput(
            label="Zadej své Minecraft jméno (IGN):",
            placeholder="Např. Adrison99",
            min_length=2,
            max_length=16,
            required=True,
        )
        self.add_item(self.ign_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        kit_key = self.kit.lower()
        ign = (self.ign_input.value or "").strip()

        # Join probíhá ATOMICky: kontrola aktivní fronty + cooldownu + duplicity
        # i samotný zápis ve stejném kritickém úseku. Dvě souběžné interakce
        # (nebo „obejití" přes modál, když cooldown mezitím naskočil) tak
        # nemůžou hráče zapsat dvakrát ani obejít cooldown.
        result = await join_queue(
            str(interaction.user.id),
            interaction.user.name,
            ign,
            self.kit,
            joined_at_ms=time.time() * 1000,
            cooldown_ms=PLAYER_COOLDOWN_MS,
        )
        status = result["result"]

        if status == "closed":
            return await interaction.response.send_message(
                "❌ Tato fronta už byla zavřena.", ephemeral=True
            )
        if status == "cooldown":
            remaining = result["remaining"]
            days = int(remaining // (24 * 60 * 60 * 1000))
            hours = int((remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000))
            return await interaction.response.send_message(
                f"❌ Máš cooldown na testy! Zkus to znovu za **{days}d {hours}h**.",
                ephemeral=True,
            )
        if status == "duplicate":
            return await interaction.response.send_message(
                "❌ V této frontě už jsi zapsaný.", ephemeral=True
            )

        await update_panel(interaction.guild, kit_key)
        await interaction.response.send_message(
            f"✅ Byl jsi úspěšně přidán do fronty **{self.kit}** s jménem `{ign}`.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Panel fronty: Join / Leave / Pull tlačítka
# ---------------------------------------------------------------------------
class QueueView(SafeView):
    def __init__(self, kit: str, disabled_join: bool = False):
        super().__init__(timeout=None)
        self.kit = kit

        join = discord.ui.Button(
            style=discord.ButtonStyle.secondary if disabled_join else discord.ButtonStyle.primary,
            label="Join Queue",
            custom_id=f"disabledjoin_{kit}" if disabled_join else f"joinbtn_{kit}",
            disabled=disabled_join,
        )
        leave = discord.ui.Button(
            style=discord.ButtonStyle.danger, label="Leave Queue", custom_id=f"leavebtn_{kit}"
        )
        pull = discord.ui.Button(
            style=discord.ButtonStyle.success, label="Pull Player ⚔️", custom_id=f"pullbtn_{kit}"
        )

        join.callback = self.on_join
        leave.callback = self.on_leave
        pull.callback = self.on_pull

        self.add_item(join)
        self.add_item(leave)
        # U zavřené fronty se Pull nezobrazuje (stejně jako v originále)
        if not disabled_join:
            self.add_item(pull)

    # ---- Join (otevře modál pro IGN) ----
    async def on_join(self, interaction: discord.Interaction) -> None:
        kit_key = self.kit.lower()
        active_queues = load_data("active_queues.json", {})
        if not active_queues.get(kit_key):
            return await interaction.response.send_message(
                "❌ Tato fronta už byla zavřena.", ephemeral=True
            )

        user_id = str(interaction.user.id)
        cooldowns = load_data("cooldowns.json", {})
        if user_id in cooldowns and (time.time() * 1000 - cooldowns[user_id]) < PLAYER_COOLDOWN_MS:
            return await interaction.response.send_message("❌ Máš cooldown na testy!", ephemeral=True)

        queue = load_data("queue.json")
        if any(
            p.get("id") == user_id and str(p.get("kit", "")).lower() == kit_key for p in queue
        ):
            return await interaction.response.send_message(
                "❌ V této frontě už jsi zapsaný.", ephemeral=True
            )

        # Předběžná kontrola je jen UX „rychlá cesta" – finální (atomická)
        # kontrola probíhá v JoinModal.on_submit.
        await interaction.response.send_modal(JoinModal(self.kit))

    # ---- Leave ----
    async def on_leave(self, interaction: discord.Interaction) -> None:
        kit_key = self.kit.lower()
        user_id = str(interaction.user.id)

        removed = await leave_queue(user_id, kit_key)
        if not removed:
            return await interaction.response.send_message(
                f"❌ Nejsi zapsaný ve frontě pro kit **{self.kit}**.", ephemeral=True
            )

        await update_panel(interaction.guild, kit_key)
        await interaction.response.send_message(
            f"✅ Úspěšně jsi opustil frontu pro kit **{self.kit}**.", ephemeral=True
        )

    # ---- Pull (výběr roomky) ----
    async def on_pull(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )

        kit_key = self.kit.lower()
        queue = load_data("queue.json")
        if not any(str(p.get("kit", "")).lower() == kit_key for p in queue):
            return await interaction.response.send_message(
                "❌ Tato fronta je prázdná, není koho vytáhnout.", ephemeral=True
            )

        select = discord.ui.ChannelSelect(
            custom_id=f"pullchannel_{kit_key}",
            placeholder="Vyber roomku pro testování...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.voice],
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_pull_channel

        view = discord.ui.View(timeout=120)
        view.add_item(select)
        await interaction.response.send_message(
            "🎯 Vyber kanál, do kterého chceš hráče vytáhnout:", view=view, ephemeral=True
        )

    async def on_pull_channel(self, interaction: discord.Interaction) -> None:
        kit_key = self.kit.lower()
        if not interaction.data.get("values"):
            return

        channel_id = int(interaction.data["values"][0])

        # Atomický pull: odebere se PRVNÍ hráč kitu. Když ho mezitím někdo
        # jiný vyřadil (odešel / pullul jiný tester / /result), řekne se to
        # narovinu a nikdo není vytažený dvakrát.
        player = await pop_for_kit(kit_key)
        if player is None:
            return await interaction.response.send_message(
                "❌ Mezitím už z fronty někdo odešel.", ephemeral=True
            )

        active_queues = load_data("active_queues.json", {})
        kit_name = active_queues.get(kit_key, {}).get("name", self.kit)
        await grant_pull_access(interaction, player, channel_id, kit_name)


# ---------------------------------------------------------------------------
# Sdílená logika pullnutí hráče do roomky (tlačítko i /queue pull)
# ---------------------------------------------------------------------------
async def grant_pull_access(
    interaction: discord.Interaction,
    player: dict,
    channel_id: int,
    kit_name: str,
) -> None:
    """Udělí hráči přístup do roomky, pošle uvítací zprávu a zaloguje pulled player.

    Pokud se práva udělit nepodaří (např. hráč není na serveru), hláška to řekne
    narovinu, ale vytažení z fronty tím není ztraceno.
    """
    guild = interaction.guild
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            channel = None

    if channel is None:
        return await interaction.response.send_message(
            "❌ Roomka se nepodařila najít. Zkus to znovu.", ephemeral=True
        )

    member = guild.get_member(int(player["id"]))
    if member is None:
        try:
            member = await guild.fetch_member(int(player["id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None

    granted = False
    if member is not None:
        try:
            await channel.set_permissions(
                member, view_channel=True, send_messages=True, connect=True, speak=True
            )
            granted = True
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning(
                "Nelze udělit práva do kanálu %s pro %s: %s",
                channel_id,
                player["id"],
                err,
            )

    # Záznam vytaženého hráče (kvůli odebrání práv po /result a kvůli /skip) –
    # nový formát uloží i info o hráči (kit/ign), aby šel vrátit na konec fronty.
    await save_pulled_player(player, channel_id)

    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(
                content=f"👋 <@{player['id']}> jsi na řadě! Tady proběhne tvůj test na kit **{kit_name}**."
            )
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning("Nelze poslat uvítací zprávu do %s: %s", channel_id, err)

    await update_panel(guild, str(player.get("kit", "")).lower())

    if granted:
        message = (
            f"✅ Hráč <@{player['id']}> byl přesunut do <#{channel_id}> a dostal práva."
        )
    else:
        message = (
            f"⚠️ Hráč <@{player['id']}> byl vytažen z fronty, ale práva se nepovedlo "
            "udělit (není hráč stále na serveru?). Bot přístup automaticky nedoplní – "
            "jakmile bude hráč na serveru, přidej ho ručně (např. přes /mktesterroom)."
        )
    await interaction.response.send_message(message, ephemeral=True)


class PullChannelSelectView(SafeView):
    """Select roomky pro /queue pull – stejný tok jako pull tlačítko na panelu."""

    def __init__(self, player: dict, kit_name: str):
        super().__init__(timeout=120)
        self.player = player
        self.kit_name = kit_name
        kit_key = str(player.get("kit", "")).lower()
        select = discord.ui.ChannelSelect(
            custom_id=f"pullcmdchannel_{kit_key}",
            placeholder="Vyber roomku pro testování...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.voice],
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        if not interaction.data.get("values"):
            return

        # Hráče z fronty vyřadíme až teď, po výběru roomky (jako u tlačítka –
        # kdyby tester roomku nevybral, hráč zůstane ve frontě). Odebrání je
        # atomické: když hráče mezitím vyřadil někdo jiný (pull/leave/result),
        # přístup se znovu neuděluje (žádný dvojitý pull).
        removed = await remove_by_player_id(self.player["id"])
        if not removed:
            return await interaction.response.send_message(
                "❌ Mezitím už z fronty někdo odešel.", ephemeral=True
            )

        await grant_pull_access(
            interaction,
            self.player,
            int(interaction.data["values"][0]),
            self.kit_name,
        )


# ---------------------------------------------------------------------------
# HT3+ tickety: kontrola tieru (limit hráče) + select menu + modál + tlačítka
# ---------------------------------------------------------------------------
# Žebříček tierů a čisté pomocné funkce (next_ticket_tier, tier_allows_tickets,
# effective_ticket_tier, find_player_tier) žijí v services/tickets.py, aby se
# daly testovat bez discord.py – viz tam.
#
# (LT5 je nejmenší, HT1 je největší.) „LT3+eval" (v kódu LT3E) je status mezi
# LT3 a HT3: hráč má pořád roli LT3, ale s evalem může otevírat HT3+ tickety.
# Evaly se drží v data/evals.json (viz utils.has_eval / set_eval / unset_eval).


def _apply_ticket_overwrites(guild, *, owner_member):
    """Overwrite kanálu ticketu: server skrytý, vlastník + tester role vidí.

    Claimer a členové (/add) se přidávají průběžně přes
    ``grant_channel_access`` / ``revoke_channel_access``.
    """
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if owner_member is not None:
        overwrites[owner_member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True
        )
    for role in get_tester_roles(guild.roles):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True
        )
    return overwrites


def ticket_embed(ticket: dict) -> discord.Embed:
    """Embed ticketu (stav, vlastník, claimer, členové) – aktualizuje se akcemi."""
    if not isinstance(ticket, dict):
        ticket = {}
    closed = ticket.get("status") != "open"
    claimer = (
        f"<@{ticket['claimerId']}>"
        if ticket.get("claimerId")
        else "— (volný)"
    )
    members = ", ".join(f"<@{m}>" for m in (ticket.get("members") or [])) or "—"
    embed = (
        discord.Embed(
            title="HT3+ Ticket Request",
            color=0xEF4444 if closed else 0x00FF00,
        )
        .add_field(name="IGN", value=ticket.get("ign") or "—", inline=False)
        .add_field(
            name="Tvůj současný tier / Požadovaný",
            value=ticket.get("targetTier") or "—",
            inline=False,
        )
        .add_field(name="GAMEMODE", value=ticket.get("kit") or "—", inline=False)
        .add_field(
            name="Tvůj aktuální tier (databáze)",
            value=ticket.get("currentTier") or "—",
            inline=False,
        )
        .add_field(name="Eval", value="✅ Ano" if ticket.get("eval") else "❌ Ne", inline=False)
        .add_field(
            name="Status",
            value="🔒 Zavřený" if closed else "🟢 Otevřený",
            inline=False,
        )
        .add_field(name="Ticket vlastní", value=f"<@{ticket.get('ownerId', '0')}>", inline=False)
        .add_field(name="Převzal (Claim)", value=claimer, inline=False)
        .add_field(name="Členové (/add)", value=members, inline=False)
    )
    return embed


class HT3PanelView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)
        kits = get_kits() or list(DEFAULT_KITS)
        select = discord.ui.Select(
            custom_id="ht3_select_kit",
            placeholder="Vyber kit pro HT3+ ticket...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=kit, value=kit) for kit in kits],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        if not interaction.data.get("values"):
            return
        selected_kit = interaction.data["values"][0]
        user_id = str(interaction.user.id)
        now = time.time() * 1000

        ht3_cooldowns = load_data("ht3_cooldowns.json", {})
        user_cd = ht3_cooldowns.get(user_id, {})
        if user_cd.get(selected_kit) and user_cd[selected_kit] > now:
            remaining = user_cd[selected_kit] - now
            days = remaining // (24 * 60 * 60 * 1000)
            hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
            return await interaction.response.send_message(
                f"❌ Na kit **{selected_kit}** máš stále HT3+ cooldown! Zbývá: **{days}d {hours}h**.",
                ephemeral=True,
            )

        await interaction.response.send_modal(HT3Modal(selected_kit))


class HT3Modal(SafeModal):
    def __init__(self, kit: str):
        super().__init__(title=f"HT3+ Ticket — {kit}")
        self.kit = kit
        self.ign_input = discord.ui.TextInput(
            label="Tvé Minecraft IGN", max_length=32, required=True
        )
        self.tier_input = discord.ui.TextInput(
            label="Na jaký chceš tier? (HT3, LT2, HT2, LT1, HT1)",
            max_length=16,
            required=True,
        )
        self.add_item(self.ign_input)
        self.add_item(self.tier_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ign = (self.ign_input.value or "").strip()
        target_tier = (self.tier_input.value or "").strip().upper()
        kit = self.kit

        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            return await interaction.followup.send("❌ Pouze na serveru.", ephemeral=True)

        # Tvarově povolené tiery pro HT3+ ticket (stejně jako label v modálu)
        if target_tier not in TIERS_UPPER:
            return await interaction.followup.send(
                "❌ Neplatný tier! Povolené tiery pro HT3+ ticket jsou: "
                "**HT3, LT2, HT2, LT1, HT1**.",
                ephemeral=True,
            )

        # Kontrola limitu: ticket nesmí být na lepší tier, než hráč může.
        # Hráč je hledaný podle IGN v players.json (stejná data, co posílá /result).
        current_tier = find_player_tier(ign, kit)
        eval_ok = has_eval(ign, kit)

        # Brána: HT3+ ticket otevřou jen hráči s „LT3+eval" (nebo HT3 a výš).
        if not eval_ok and not tier_allows_tickets(current_tier):
            return await interaction.followup.send(
                "❌ **Bez evalu nelze otevřít HT3+ ticket!**\n"
                "Eval dostaneš, když **porazíš LT3 testera** (nebo když tvůj "
                "tester usoudí, že máš HT3 skill). Je to status mezi LT3 a HT3 "
                "– roli máš pořád LT3, ale můžeš otevírat HT3+ tickety.",
                ephemeral=True,
            )

        effective_tier = effective_ticket_tier(current_tier, eval_ok)
        limit_tier = next_ticket_tier(effective_tier) if effective_tier else None
        if effective_tier and limit_tier and target_tier in HT3_TIER_LADDER:
            limit_idx = HT3_TIER_LADDER.index(limit_tier)
            typed_idx = HT3_TIER_LADDER.index(target_tier)
            if typed_idx > limit_idx:
                return await interaction.followup.send(
                    f"❌ **Ticket na `{target_tier}` přesahuje tvůj limit!**\n"
                    f"Tvůj aktuální tier v kitu **{kit}** je `{current_tier}` – "
                    f"maximálně můžeš jít na **{limit_tier}**. Uprav ticket prosím "
                    f"na `{limit_tier}` (nebo retest na `{current_tier}`).",
                    ephemeral=True,
                )

        # Prevence duplicit (rychlá kontrola; autoritativní je uvnitř
        # create_ticket v transaction – tam se případné souběžné vytvoření
        # stejného hráče + kitu pozná a přepíše tento kanál).
        existing = await find_open_ticket(str(interaction.user.id), kit)
        if existing is not None:
            return await interaction.followup.send(
                f"❌ Už máš otevřený HT3+ ticket pro kit **{kit}**: "
                f"<#{existing.get('id')}>. Nejprve ho zavři.",
                ephemeral=True,
            )

        guild = interaction.guild
        owner_member = guild.get_member(interaction.user.id)
        if owner_member is None:
            try:
                owner_member = await guild.fetch_member(interaction.user.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                owner_member = None

        # Kategorie podle tieru, pak podle kitu, jinak výchozí (configurable)
        category_id = get_ht3_ticket_category(target_tier, kit)
        category = guild.get_channel(category_id)
        if category is None:
            try:
                category = await guild.fetch_channel(category_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                category = None
        if category is None:
            return await interaction.followup.send(
                f"❌ Kategorie pro ticket (ID `{category_id}`) nebyla nalezena!",
                ephemeral=True,
            )

        channel = await guild.create_text_channel(
            name=f"{ign}-{target_tier}-{kit}".lower(),
            category=category,
            overwrites=_apply_ticket_overwrites(guild, owner_member=owner_member),
        )

        ticket = None
        result = await create_ticket(
            channel_id=channel.id,
            owner_id=str(interaction.user.id),
            owner_name=interaction.user.display_name or interaction.user.name,
            ign=ign,
            kit=kit,
            target_tier=target_tier,
            current_tier=current_tier,
            eval_ok=eval_ok,
            category_id=category_id,
            now=int(time.time() * 1000),
        )
        if result["result"] == "duplicate":
            # Závod: ticket pro stejný kit mezitím vznikl jinde – tenhle kanál
            # je prázdný, smažeme ho a pošleme odkaz na existující ticket.
            try:
                await channel.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            existing_dup = result["ticket"]
            return await interaction.followup.send(
                f"❌ Už máš otevřený HT3+ ticket pro kit **{kit}**: "
                f"<#{existing_dup.get('id')}>. Nejprve ho zavři.",
                ephemeral=True,
            )
        ticket = result["ticket"]

        embed = ticket_embed(ticket)
        ticket_view = HTTicketView()
        message = await channel.send(
            content=f"<@{interaction.user.id}>", embed=embed, view=ticket_view
        )

        # Registrace persistentní view – tlačítka ticketu přežijí restart
        # (interaction.client = bot; u nové registrace i po restartu).
        try:
            interaction.client.add_view(ticket_view, message_id=message.id)
        except (ValueError, discord.ClientException) as err:
            log.warning("Nelze zaregistrovat view ticketu %s: %s", channel.id, err)

        # Do záznamu doplníme ID panel zprávy (restart-safe readd view)
        await set_panel_message(channel.id, str(message.id))

        await log_ticket_event(
            channel.id,
            "created",
            str(interaction.user.id),
            interaction.user.display_name or interaction.user.name,
            details=f"Ticket {target_tier} / {kit} (IGN {ign})",
        )

        note = ""
        if current_tier and limit_tier and target_tier != limit_tier:
            note = (
                f"\n💡 Tvůj aktuální tier je `{current_tier}` – "
                f"garantovaný další tier je `{limit_tier}`."
            )
        await interaction.followup.send(
            f"Ticket byl vytvořen: <#{channel.id}>{note}", ephemeral=True
        )


class HTTicketView(SafeView):
    """Persistentní tlačítka ticketu: Claim HT / Unclaim / Close / Reopen.

    View je bezstavový – stav se čte z ``ht_tickets.json`` podle ID kanálu
    (``interaction.channel_id``), takže stejně funguje i po restartu bota.
    """

    def __init__(self):
        super().__init__(timeout=None)
        claim = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="✅ Claim HT",
            custom_id="ht_claim",
        )
        unclaim = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="↩️ Unclaim",
            custom_id="ht_unclaim",
        )
        close = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="🔒 Close Ticket",
            custom_id="ht_close",
        )
        reopen = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="🔓 Reopen Ticket",
            custom_id="ht_reopen",
        )
        claim.callback = self.on_claim
        unclaim.callback = self.on_unclaim
        close.callback = self.on_close
        reopen.callback = self.on_reopen
        self.add_item(claim)
        self.add_item(unclaim)
        self.add_item(close)
        self.add_item(reopen)

    @staticmethod
    async def _active_ticket(interaction) -> dict | None:
        ticket = await get_ticket(interaction.channel_id)
        if ticket is None:
            await _ticket_not_found(interaction)
            return None
        return ticket

    async def _refresh(self, interaction, ticket) -> None:
        """Aktualizuje embed panel zprávy ticketu (pokud existuje)."""
        try:
            message = interaction.message
            if message is not None:
                await message.edit(embed=ticket_embed(ticket))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def on_claim(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        ticket = await self._active_ticket(interaction)
        if ticket is None:
            return
        result = await claim_ticket(
            interaction.channel_id, str(interaction.user.id), interaction.user.display_name
        )
        r = result["result"]
        if r == "not_open":
            return await interaction.response.send_message(
                "❌ Ticket je zavřený – nejdřív ho otevři (Reopen).", ephemeral=True
            )
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

        ticket = result["ticket"]
        await grant_channel_access(interaction.channel, str(interaction.user.id))
        await log_ticket_event(
            interaction.channel_id, "claimed", str(interaction.user.id),
            interaction.user.display_name,
            details=f"Claim: {ticket.get('ign')} / {ticket.get('kit')}",
        )
        await self._refresh(interaction, ticket)
        await interaction.response.send_message(
            f"✅ **{interaction.user.display_name}** převzal/a ticket "
            f"<#{interaction.channel_id}> – můžeš začít test.",
            ephemeral=True,
        )

    async def on_unclaim(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        ticket = await self._active_ticket(interaction)
        if ticket is None:
            return
        result = await unclaim_ticket(
            interaction.channel_id, str(interaction.user.id), force=True
        )
        if result["result"] != "unclaimed":
            return await interaction.response.send_message(
                "❌ Ticket nemá nikdo převzatý (nebo se nepodařilo uvolnit).",
                ephemeral=True,
            )
        previous = result["previous"]
        await revoke_channel_access(interaction.channel, previous["claimer_id"])
        await log_ticket_event(
            interaction.channel_id, "unclaimed", str(interaction.user.id),
            interaction.user.display_name,
            details=f"Vzdal se: {previous['claimer_name'] or previous['claimer_id']}",
        )
        await self._refresh(interaction, result["ticket"])
        await interaction.response.send_message(
            "↩️ Ticket je zase volný – nikdo ho nemá převzatý.", ephemeral=True
        )

    async def on_close(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        ticket = await self._active_ticket(interaction)
        if ticket is None:
            return
        result = await close_ticket(
            interaction.channel_id,
            str(interaction.user.id),
            cooldown_ms=HT3_COOLDOWN_MS,
        )
        r = result["result"]
        if r == "already_closed":
            return await interaction.response.send_message(
                "❌ Ticket už je zavřený.", ephemeral=True
            )
        if r != "closed":
            return await interaction.response.send_message(
                "❌ Ticket se nepodařilo zavřít.", ephemeral=True
            )
        await log_ticket_event(
            interaction.channel_id, "closed", str(interaction.user.id),
            interaction.user.display_name,
            details="7denní HT3+ cooldown nastaven",
        )
        await self._refresh(interaction, result["ticket"])
        await interaction.response.send_message(
            "🔒 Ticket zavřený – hráč má 7denní HT3+ cooldown na tento kit. "
            "Kanál zůstává (Reopen / log).",
            ephemeral=True,
        )

    async def on_reopen(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        ticket = await self._active_ticket(interaction)
        if ticket is None:
            return
        result = await reopen_ticket(interaction.channel_id, str(interaction.user.id))
        if result["result"] == "not_closed":
            return await interaction.response.send_message(
                "❌ Ticket už je otevřený.", ephemeral=True
            )
        if result["result"] != "reopened":
            return await interaction.response.send_message(
                "❌ Ticket se nepodařilo znovu otevřít.", ephemeral=True
            )
        await log_ticket_event(
            interaction.channel_id, "reopened", str(interaction.user.id),
            interaction.user.display_name,
        )
        await self._refresh(interaction, result["ticket"])
        await interaction.response.send_message(
            "🔓 Ticket je zase otevřený.", ephemeral=True
        )


async def _ticket_not_found(interaction) -> None:
    await interaction.response.send_message(
        "❌ Tento kanál není HT ticket (záznam chybí).", ephemeral=True
    )


async def grant_channel_access(channel, user_id: str) -> None:
    """Přidá uživateli explicitní přístup do kanálu ticketu (best effort)."""
    guild = getattr(channel, "guild", None)
    member = guild.get_member(int(user_id)) if guild else None
    if member is None:
        return
    try:
        await channel.set_permissions(
            member, view_channel=True, send_messages=True
        )
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze udělit přístup do ticketu %s pro %s: %s", channel.id, user_id, err)


async def revoke_channel_access(channel, user_id: str) -> None:
    """Odebere explicitní přístup uživatele do kanálu ticketu (best effort)."""
    guild = getattr(channel, "guild", None)
    member = guild.get_member(int(user_id)) if guild else None
    if member is None:
        return
    try:
        await channel.set_permissions(member, overwrite=None)
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze odebrat přístup do ticketu %s pro %s: %s", channel.id, user_id, err)


# ---------------------------------------------------------------------------
# Turnaj: tlačítko Přihlásit se
# ---------------------------------------------------------------------------
class TournamentSignupView(SafeView):
    def __init__(self, kit_key: str):
        super().__init__(timeout=None)
        self.kit_key = kit_key
        btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="✅ Přihlásit se",
            custom_id=f"signup_turnaj_{kit_key}",
        )
        btn.callback = self.on_signup
        self.add_item(btn)

    async def on_signup(self, interaction: discord.Interaction) -> None:
        tournaments = load_data("tournaments.json", {})
        tdata = tournaments.get(self.kit_key)
        if not tdata or tdata.get("ended"):
            return await interaction.response.send_message(
                "Přihlašování do tohoto turnaje již skončilo.", ephemeral=True
            )

        user_id = str(interaction.user.id)
        participants = tdata.setdefault("participants", [])
        if user_id in participants:
            return await interaction.response.send_message(
                "Už jsi v tomto turnaji přihlášen.", ephemeral=True
            )

        participants.append(user_id)
        save_data("tournaments.json", tournaments)
        await interaction.response.send_message(
            "Byl jsi úspěšně přihlášen do turnaje! ✅", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Tester roomka: tlačítko 🔒 Zavřít místnost (smaže kanál)
# ---------------------------------------------------------------------------
class TesterRoomView(SafeView):
    """Tlačítko pro zavření tester roomky (vytvořené přes /mktesterroom)."""

    def __init__(self):
        super().__init__(timeout=None)
        btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="🔒 Zavřít místnost",
            custom_id="close_testerroom",
        )
        btn.callback = self.on_close
        self.add_item(btn)

    async def on_close(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Jen tester může zavřít místnost.", ephemeral=True
            )

        await interaction.response.send_message("🔒 Místnost se zavírá...")

        channel = interaction.channel

        async def _delete_later() -> None:
            await asyncio.sleep(3)
            try:
                await channel.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        asyncio.create_task(_delete_later())
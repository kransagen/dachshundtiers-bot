"""Interaktivní komponenty: tlačítka, select menu a modály.

Odpovídají původním discord.js interakcím (joinbtn / leavebtn / pullbtn,
joinmodal, ht3_select_kit, close_ht3, signup_turnaj).

HT3 panel už nemá vlastní modál: hráč vybere jen kit a vše ostatní (IGN,
tier, cílový tier, duplikáty) se odvodí v ``services.ht3_tickets`` a tady
se jen provedou Discord side effecty přes ``_open_ht3_ticket``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import discord

from config import HT3_COOLDOWN_MS, PLAYER_COOLDOWN_MS, get_ht3_ticket_category
from db.repositories.kits import KitRepository
from db.repositories.tournaments import TournamentRepository
from db.services.session import transaction
from panel import update_panel
from services.cooldowns import get_cooldowns
from services.ht3_tickets import resolve_ht3_context
from services.permissions import get_tester_roles
from services.queue_service import (
    PULL_EMPTY,
    PULL_NO_KIT,
    PULL_NO_ROOM,
    join_queue,
    leave_queue,
    list_queue_entries,
    pull_for_kit,
    queue_state,
    save_pulled_player,
)
from services.tickets import (
    claim_ticket,
    close_ticket,
    create_ticket,
    get_ticket,
    log_ticket_event,
    reopen_ticket,
    set_panel_message,
    unclaim_ticket,
)
from utils import DEFAULT_KITS, has_tester_role, spawn

log = logging.getLogger("dachshundtiers")


def _session_factory(interaction: discord.Interaction):
    """session_factory z bota (None v JSON režimu/testech)."""
    return getattr(getattr(interaction, "client", None), "db_session_factory", None)


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
            session_factory=_session_factory(interaction),
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

        await update_panel(
            interaction.guild, kit_key, session_factory=_session_factory(interaction)
        )
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
        session_factory = _session_factory(interaction)
        if await queue_state(kit_key, session_factory=session_factory) is None:
            return await interaction.response.send_message(
                "❌ Tato fronta už byla zavřena.", ephemeral=True
            )

        user_id = str(interaction.user.id)
        cooldowns = await get_cooldowns(
            user_id,
            session_factory=session_factory,
            waitlist_cooldown_ms=PLAYER_COOLDOWN_MS,
        )
        if cooldowns["waitlist_ms"] is not None:
            return await interaction.response.send_message("❌ Máš cooldown na testy!", ephemeral=True)

        queue = await list_queue_entries(kit_key, session_factory=session_factory)
        if any(
            str(p.get("id")) == user_id and str(p.get("kit", "")).lower() == kit_key for p in queue
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

        removed = await leave_queue(
            user_id, kit_key, session_factory=_session_factory(interaction)
        )
        if not removed:
            return await interaction.response.send_message(
                f"❌ Nejsi zapsaný ve frontě pro kit **{self.kit}**.", ephemeral=True
            )

        await update_panel(
            interaction.guild, kit_key, session_factory=_session_factory(interaction)
        )
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
        # Roomka patří kitu (`/mktesterroom <kit>`), tester ji znovu nevybírá –
        # jinak by existovaly dvě cesty, kam hráče poslat, a mapování v
        # `kit_tester_rooms` by nebylo autoritativní. Stejná služba jako
        # `/queue pull <kit>`: obojí jde přes `pull_for_kit`.
        result = await pull_for_kit(kit_key, session_factory=_session_factory(interaction))

        if result.status == PULL_NO_ROOM:
            return await interaction.response.send_message(
                f"❌ Kit **{self.kit}** nemá tester roomku – vytvoř ji "
                f"`/mktesterroom {kit_key}`. Hráč ve frontě zůstal.",
                ephemeral=True,
            )
        if result.status == PULL_NO_KIT:
            return await interaction.response.send_message(
                f"❌ Kit `{self.kit}` neznám.", ephemeral=True
            )
        if result.status == PULL_EMPTY:
            return await interaction.response.send_message(
                "❌ Tato fronta je prázdná, není koho vytáhnout.", ephemeral=True
            )

        await grant_pull_access(
            interaction,
            result.player,
            result.channel_id,
            result.kit_name,
            session_factory=_session_factory(interaction),
        )


# ---------------------------------------------------------------------------
# Sdílená logika pullnutí hráče do roomky (tlačítko i /queue pull)
# ---------------------------------------------------------------------------
async def grant_pull_access(
    interaction: discord.Interaction,
    player: dict,
    channel_id: int,
    kit_name: str,
    *,
    session_factory=None,
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

    # Záznam vytaženého hráče (kvůli odebrání práv po /result a kvůli /skip).
    await save_pulled_player(player, channel_id, session_factory=session_factory)

    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(
                content=f"👋 <@{player['id']}> jsi na řadě! Tady proběhne tvůj test na kit **{kit_name}**."
            )
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning("Nelze poslat uvítací zprávu do %s: %s", channel_id, err)

    await update_panel(
        guild, str(player.get("kit", "")).lower(), session_factory=session_factory
    )

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


# ---------------------------------------------------------------------------
# HT3+ tickety: výběr kitu na panelu + tlačítka ticketu
# ---------------------------------------------------------------------------
# Žebříček tierů a čisté pomocné funkce (next_ticket_tier, tier_allows_tickets,
# effective_ticket_tier) žijí v services/tickets.py, aby se daly testovat bez
# discord.py – viz tam.
#
# (LT5 je nejmenší, HT1 je největší.) „LT3+eval" (v kódu LT3E) je status mezi
# LT3 a HT3: hráč má pořád roli LT3, ale s evalem může otevírat HT3+ tickety.
#
# Panel už hráče NIC nenechává vypisovat: vybere jen KIT a vše ostatní se
# odvodí v services/ht3_tickets.py (IGN z propojeného Minecraft účtu, tier z
# potvrzeného stavu v databázi, cílový tier ze žebříčku). Dřív to panel dělal
# TextInputem pro IGN i cílový tier, takže si hráč mohl otevřít ticket pro
# cizí účet nebo pro tier, na který neměl nárok.
#
# TICKETY SE OTEVÍRAJÍ JEDNOU, přes _open_ht3_ticket(): panel výběrem kitu i
# /seteval, který po evalu rovnou založí HT3 ticket. Dvě cesty do stejného
# kanálu by znamenaly dvě místa, kde se dá rozhodnout „tady ticket vznikne" – a
# právě tam se při opakovaném zpracování duplikoval.


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


# ---------------------------------------------------------------------------
# Otevření HT3+ ticketu – JEDNO místo pro obě cesty
# ---------------------------------------------------------------------------
# Panel (výběr kitu) a /seteval (eval právě proběhl) oba potřebují založit
# stejný ticket. Kdyby to byla dvě implementace, rozdělily by se v detailu
# (např. jen jedna by uměla uklidit kanál po závodě) a „ticket se zakládá
# dvakrát" by bylo možné jen v jedné z nich. Tady je to jedno.

TICKET_OPENED = "opened"
TICKET_DUPLICATE = "duplicate"
TICKET_NO_CATEGORY = "no_category"
TICKET_FAILED = "failed"


@dataclass(frozen=True)
class HT3TicketOpened:
    status: str
    channel_id: Optional[int] = None
    ticket: Optional[dict] = None
    message: Optional[str] = None


async def _open_ht3_ticket(
    interaction,
    *,
    ign: str,
    kit: str,
    target_tier: str,
    current_tier: Optional[str],
    eval_ok: bool,
    owner_id: str,
    session_factory=None,
) -> HT3TicketOpened:
    """Create the ticket channel + row + panel, and clean up on a lost race.

    IGN, ``target_tier`` and ``current_tier`` are already resolved by the
    caller (``services.ht3_tickets``); this function performs the Discord
    side effects and the single database write.
    """
    guild = interaction.guild
    if guild is None:
        return HT3TicketOpened(
            status=TICKET_FAILED, message="❌ Pouze na serveru."
        )

    owner_member = guild.get_member(int(owner_id))
    if owner_member is None:
        try:
            owner_member = await guild.fetch_member(int(owner_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            owner_member = None

    category_id = get_ht3_ticket_category(target_tier, kit)
    category = guild.get_channel(category_id)
    if category is None:
        try:
            category = await guild.fetch_channel(category_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            category = None
    if category is None:
        return HT3TicketOpened(
            status=TICKET_NO_CATEGORY,
            message=(
                f"❌ Kategorie pro ticket (ID `{category_id}`) nebyla nalezena! "
                "Zkontroluj `HT3_TICKET_CATEGORY_*` v konfiguraci."
            ),
        )

    try:
        channel = await guild.create_text_channel(
            name=f"{ign}-{target_tier}-{kit}".lower(),
            category=category,
            overwrites=_apply_ticket_overwrites(guild, owner_member=owner_member),
        )
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze vytvořit kanál ticketu (%s/%s): %s", ign, target_tier, err)
        return HT3TicketOpened(
            status=TICKET_FAILED,
            message="❌ Kanál ticketu se nepodařilo vytvořit (zkontroluj oprávnění bota).",
        )

    result = await create_ticket(
        channel_id=channel.id,
        owner_id=owner_id,
        owner_name=interaction.user.display_name or interaction.user.name,
        ign=ign,
        kit=kit,
        target_tier=target_tier,
        current_tier=current_tier,
        eval_ok=eval_ok,
        category_id=category_id,
        now=int(time.time() * 1000),
        session_factory=session_factory,
    )
    if result["result"] != "created":
        # Lost the race: an open ticket for this player+kit already exists
        # (uq_tickets_open_player_kit). The channel we just made is empty and
        # unreferenced, so delete it rather than leave an orphan room.
        try:
            await channel.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        existing = result.get("ticket") or {}
        if result["result"] == "duplicate":
            return HT3TicketOpened(
                status=TICKET_DUPLICATE,
                ticket=existing,
                message=(
                    f"ℹ️ Už máš otevřený HT3+ ticket pro kit **{kit}**: "
                    f"<#{existing.get('id')}>. Nejprve ho zavři."
                ),
            )
        # identity_conflict / invalid_kit – explain rather than fail silently.
        return HT3TicketOpened(
            status=TICKET_FAILED,
            message=f"❌ Ticket nevznikl: {result.get('message') or result['result']}",
        )

    ticket = result["ticket"]
    embed = ticket_embed(ticket)
    ticket_view = HTTicketView()
    try:
        message = await channel.send(
            content=f"<@{owner_id}>", embed=embed, view=ticket_view
        )
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze poslat panel ticketu do %s: %s", channel.id, err)
        message = None

    if message is not None:
        # Register the persistent view so the buttons survive a restart.
        client = getattr(interaction, "client", None)
        if client is not None:
            try:
                client.add_view(ticket_view, message_id=message.id)
            except (ValueError, AttributeError, discord.ClientException) as err:
                log.warning("Nelze zaregistrovat view ticketu %s: %s", channel.id, err)

        await set_panel_message(
            channel.id, str(message.id), session_factory=session_factory
        )

    await log_ticket_event(
        channel.id,
        "created",
        owner_id,
        interaction.user.display_name or interaction.user.name,
        details=f"Ticket {target_tier} / {kit} (IGN {ign})",
        session_factory=session_factory,
    )

    return HT3TicketOpened(
        status=TICKET_OPENED, channel_id=channel.id, ticket=ticket
    )


class HT3PanelView(SafeView):
    """Select menu panelu HT3+.

    ``kits`` se musí předat zvenku – načítá ho volající přes
    ``services.kit_catalog.get_kits`` (PostgreSQL). Tady se žádný soubor
    nečte: ``__init__`` běží synchronně a nemá session factory, takže by
    musel číst JSON, který už neexistí jako zdroj pravdy. Všichni tři
    volající (``cogs/kits.py``, ``cogs/ht3.py``, ``bot.py``) katalog
    předávají.
    """

    def __init__(self, kits: list[str] | None = None):
        super().__init__(timeout=None)
        kits = list(kits) if kits else list(DEFAULT_KITS)
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
        session_factory = _session_factory(interaction)

        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        user_cd = await get_cooldowns(user_id, session_factory=session_factory)
        remaining = user_cd["ht3"].get(selected_kit)
        if remaining:
            days = remaining // (24 * 60 * 60 * 1000)
            hours = (remaining % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000)
            return await interaction.response.send_message(
                f"❌ Na kit **{selected_kit}** máš stále HT3+ cooldown! Zbývá: **{days}d {hours}h**.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        # Vše ostatní (IGN, tier, cílový tier, duplikáty) se odvodí z DB.
        # Bez propojeného Minecraft účtu nebo bez uloženého tieru se ticket
        # NEZAKLÁDÁ – hráč dostane konkrétní hlášku, co mu chybí.
        context = await resolve_ht3_context(
            int(interaction.user.id), selected_kit, session_factory=session_factory
        )
        if not context.ok:
            return await interaction.followup.send(context.message, ephemeral=True)

        opened = await _open_ht3_ticket(
            interaction,
            ign=context.ign,
            kit=context.kit,
            target_tier=context.target_tier,
            current_tier=context.current_tier,
            eval_ok=context.eval_ok,
            owner_id=str(interaction.user.id),
            session_factory=session_factory,
        )
        if opened.status != TICKET_OPENED:
            return await interaction.followup.send(
                opened.message or "❌ HT3+ ticket nejde vytvořit.", ephemeral=True
            )

        note = ""
        if context.current_tier and context.target_tier:
            note = (
                f"\n💡 Tvůj aktuální tier je `{context.current_tier}`, "
                f"ticket jde na `{context.target_tier}` (maximum ze žebříčku)."
            )
        await interaction.followup.send(
            f"✅ Ticket byl vytvořen: <#{opened.channel_id}>{note}", ephemeral=True
        )


class HTTicketView(SafeView):
    """Persistentní tlačítka ticketu: Claim HT / Unclaim / Close / Reopen.

    View je bezstavový – stav se čte z PostgreSQL (F10) podle ID kanálu
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
        ticket = await get_ticket(
            interaction.channel_id,
            session_factory=_session_factory(interaction),
        )
        if ticket is None:
            await _ticket_not_found(interaction)
            return None
        return ticket

    async def _refresh_ticket_state(self, interaction, ticket) -> None:
        """Aktualizuje embed panel zprávy ticketu (pokud existuje).

        Pozn.: pojmenováno tak, aby NEstínilo interní ``discord.ui.View._refresh(components)``
        (volané z ``ViewStore.update_from_message`` při MESSAGE_UPDATE).
        """
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
            interaction.channel_id, str(interaction.user.id),
            interaction.user.display_name,
            session_factory=_session_factory(interaction),
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
            session_factory=_session_factory(interaction),
        )
        await self._refresh_ticket_state(interaction, ticket)
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
            interaction.channel_id, str(interaction.user.id), force=True,
            session_factory=_session_factory(interaction),
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
            session_factory=_session_factory(interaction),
        )
        await self._refresh_ticket_state(interaction, result["ticket"])
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
            session_factory=_session_factory(interaction),
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
            session_factory=_session_factory(interaction),
        )
        await self._refresh_ticket_state(interaction, result["ticket"])
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
        result = await reopen_ticket(
            interaction.channel_id,
            str(interaction.user.id),
            cooldown_ms=HT3_COOLDOWN_MS,
            session_factory=_session_factory(interaction),
        )
        if result["result"] == "not_closed":
            return await interaction.response.send_message(
                "❌ Ticket už je otevřený.", ephemeral=True
            )
        if result["result"] == "cooldown":
            remaining = int(result.get("remaining_ms") or 0)
            days, rem = divmod(remaining, 86_400_000)
            hours, rem = divmod(rem, 3_600_000)
            mins = rem // 60_000
            bits = []
            if days:
                bits.append(f"{days} d")
            if hours:
                bits.append(f"{hours} h")
            if mins:
                bits.append(f"{mins} min")
            duration = " ".join(bits) or "méně než minutu"
            return await interaction.response.send_message(
                "❌ **HT3+ cooldown ještě neskončil** – ticket lze znovu "
                f"otevřít za **{duration}** (kit `{result.get('kit') or '?'}`).",
                ephemeral=True,
            )
        if result["result"] != "reopened":
            return await interaction.response.send_message(
                "❌ Ticket se nepodařilo znovu otevřít.", ephemeral=True
            )
        await log_ticket_event(
            interaction.channel_id, "reopened", str(interaction.user.id),
            interaction.user.display_name,
            session_factory=_session_factory(interaction),
        )
        await self._refresh_ticket_state(interaction, result["ticket"])
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
        session_factory = getattr(
            getattr(interaction, "client", None), "db_session_factory", None
        )
        if session_factory is None:
            return await interaction.response.send_message(
                "⚠️ Databáze není dostupná — zkus to později.", ephemeral=True
            )
        async with transaction(session_factory) as session:
            kit = await KitRepository().get_by_key(session, self.kit_key)
            if kit is None:
                return await interaction.response.send_message(
                    "Turnaj pro tento kit neexistuje.", ephemeral=True
                )
            tournament = await TournamentRepository().get_by_kit(
                session, kit_id=kit.id
            )
            if tournament is None or tournament.ended:
                return await interaction.response.send_message(
                    "Přihlašování do tohoto turnaje již skončilo.", ephemeral=True
                )
            if await TournamentRepository().is_participant(
                session, tournament_id=tournament.id, player_id=interaction.user.id
            ):
                return await interaction.response.send_message(
                    "Už jsi v tomto turnaji přihlášen.", ephemeral=True
                )
            await TournamentRepository().add_participant(
                session, tournament_id=tournament.id, player_id=interaction.user.id
            )
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

        spawn(_delete_later(), name="close-testerroom")
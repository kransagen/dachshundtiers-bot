"""Interaktivní komponenty: tlačítka, select menu a modály.

Odpovídají původním discord.js interakcím (joinbtn / leavebtn / pullbtn,
joinmodal, ht3_select_kit, ht3_modal, close_ht3, signup_turnaj).
"""

import asyncio
import logging
import time

import discord

from config import HT3_COOLDOWN_MS, PLAYER_COOLDOWN_MS, get_ht3_ticket_category
from panel import update_panel
from storage import load_data, save_data
from utils import DEFAULT_KITS, get_kits, has_tester_role

log = logging.getLogger("dachshundtiers")


# ---------------------------------------------------------------------------
# Modál pro zadání IGN při připojení do fronty (joinbtn → joinmodal)
# ---------------------------------------------------------------------------
class JoinModal(discord.ui.Modal):
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
        active_queues = load_data("active_queues.json", {})
        if not active_queues.get(kit_key):
            return await interaction.response.send_message(
                "❌ Tato fronta už byla zavřena.", ephemeral=True
            )

        ign = (self.ign_input.value or "").strip()
        user_id = str(interaction.user.id)
        queue = load_data("queue.json")
        queue.append(
            {
                "id": user_id,
                "username": interaction.user.name,
                "ign": ign,
                "kit": self.kit,
                "joinedAt": time.time() * 1000,
                "testerId": None,
            }
        )
        save_data("queue.json", queue)

        await update_panel(interaction.guild, kit_key)
        await interaction.response.send_message(
            f"✅ Byl jsi úspěšně přidán do fronty **{self.kit}** s jménem `{ign}`.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Panel fronty: Join / Leave / Pull tlačítka
# ---------------------------------------------------------------------------
class QueueView(discord.ui.View):
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

        await interaction.response.send_modal(JoinModal(self.kit))

    # ---- Leave ----
    async def on_leave(self, interaction: discord.Interaction) -> None:
        kit_key = self.kit.lower()
        user_id = str(interaction.user.id)
        queue = load_data("queue.json")
        new_queue = [
            p
            for p in queue
            if not (p.get("id") == user_id and str(p.get("kit", "")).lower() == kit_key)
        ]

        if len(new_queue) == len(queue):
            return await interaction.response.send_message(
                f"❌ Nejsi zapsaný ve frontě pro kit **{self.kit}**.", ephemeral=True
            )

        save_data("queue.json", new_queue)
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

        queue = load_data("queue.json")
        index = next(
            (i for i, p in enumerate(queue) if str(p.get("kit", "")).lower() == kit_key), None
        )
        if index is None:
            return await interaction.response.send_message(
                "❌ Mezitím už z fronty někdo odešel.", ephemeral=True
            )

        player = queue.pop(index)
        save_data("queue.json", queue)

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

    # Záznam vytaženého hráče (kvůli odebrání práv po /result)
    pulled = load_data("pulled_players.json", {})
    pulled[player["id"]] = str(channel_id)
    save_data("pulled_players.json", pulled)

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
            "udělit (není hráč stále na serveru?). Do roomky přidám hráče hned, "
            "jakmile server opět uvidí."
        )
    await interaction.response.send_message(message, ephemeral=True)


class PullChannelSelectView(discord.ui.View):
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
        # kdyby tester roomku nevybral, hráč zůstane ve frontě).
        queue = load_data("queue.json")
        new_queue = [
            p for p in queue if p.get("id") != self.player["id"]
        ]
        if len(new_queue) != len(queue):
            save_data("queue.json", new_queue)

        await grant_pull_access(
            interaction,
            self.player,
            int(interaction.data["values"][0]),
            self.kit_name,
        )


# ---------------------------------------------------------------------------
# HT3+ tickety: select menu + modál + tlačítko Close Ticket
# ---------------------------------------------------------------------------
class HT3PanelView(discord.ui.View):
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


class HT3Modal(discord.ui.Modal):
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
        target_tier = (self.tier_input.value or "").strip()
        kit = self.kit

        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            return await interaction.followup.send("❌ Pouze na serveru.", ephemeral=True)

        guild = interaction.guild
        everyone = guild.default_role

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
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                ),
            },
        )

        embed = (
            discord.Embed(title="HT3+ Ticket Request", color=0x00FF00)
            .add_field(name="IGN", value=ign, inline=False)
            .add_field(name="Tvůj současný tier / Požadovaný", value=target_tier, inline=False)
            .add_field(name="GAMEMODE", value=kit, inline=False)
        )

        close_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="🔒 Close Ticket",
            custom_id=f"close_ht3_{interaction.user.id}_{kit}",
        )
        close_btn.callback = self.on_close
        ticket_view = discord.ui.View(timeout=None)
        ticket_view.add_item(close_btn)

        await channel.send(content=f"<@{interaction.user.id}>", embed=embed, view=ticket_view)
        await interaction.followup.send(
            f"Ticket byl vytvořen: <#{channel.id}>", ephemeral=True
        )

    async def on_close(self, interaction: discord.Interaction) -> None:
        # custom_id: close_ht3_{userId}_{kit}
        parts = interaction.data.get("custom_id", "").split("_")
        try:
            user_id = parts[2]
        except IndexError:
            user_id = ""
        kit = "_".join(parts[3:]) if len(parts) > 3 else ""

        ht3_cooldowns = load_data("ht3_cooldowns.json", {})
        ht3_cooldowns.setdefault(user_id, {})[kit] = time.time() * 1000 + HT3_COOLDOWN_MS
        save_data("ht3_cooldowns.json", ht3_cooldowns)

        await interaction.response.send_message(
            "Ticket se zavírá a byl nastaven 7denní cooldown..."
        )

        channel = interaction.channel

        async def _delete_later() -> None:
            await asyncio.sleep(3)
            try:
                await channel.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        asyncio.create_task(_delete_later())


# ---------------------------------------------------------------------------
# Turnaj: tlačítko Přihlásit se
# ---------------------------------------------------------------------------
class TournamentSignupView(discord.ui.View):
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
class TesterRoomView(discord.ui.View):
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
"""Cog se správou testovacích front: /openq, /closeq, /queue ..., /removeq.

Port původních funkcí z DACHSHUNDTIERSQBOT (JS):
- /openq  – otevření fronty s živým panelem a tlačítky
- /closeq – uzavření fronty
- /queue join / list / joinastester / joinasqueue / leaveq / pull
- /removeq
"""

import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import PLAYER_COOLDOWN_MS, TESTER_ROOM_CATEGORY_ID, get_queue_channel_id
from panel import create_queue_embed, update_panel
from services.queue_service import join_queue, save_pulled_player
from services.store import transaction
from storage import load_data, save_data
from utils import has_tester_role, kit_autocomplete
from views import PullChannelSelectView, QueueView, TesterRoomView

log = logging.getLogger("dachshundtiers")


def _move_to_queue_end(queue: list, stored_player, player_id: str):
    """Přesune hráče na konec fronty (ostatní jdou před něj).

    Vrací ``(new_queue, kit_key, moved)``. Pokud hráč ve frontě není,
    použije se uložený záznam z pullnutí (``stored_player``).
    """
    kit_key = ""
    entry_to_move = stored_player if (stored_player and stored_player.get("kit")) else None
    new_queue = []
    moved = False
    for p in queue:
        if str(p.get("id", "")) == player_id and not moved:
            entry_to_move = p
            moved = True
            continue
        new_queue.append(p)
    if entry_to_move and entry_to_move.get("kit"):
        new_queue.append(entry_to_move)
        kit_key = str(entry_to_move["kit"]).lower()
    return new_queue, kit_key, moved


class Queues(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /openq
    # ------------------------------------------------------------------
    @app_commands.command(name="openq", description="Open a specific kit queue")
    @app_commands.describe(kit="Name of the kit/queue")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def openq(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Only testers can open queues.", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        kit_key = kit.lower()

        # Otevření fronty je atomické: kontrola, že už není otevřená, i zápis
        # proběhnou v jednom kritickém úseku (žádné dvojité otevření).
        async def _open(tx):
            active_queues = tx.get("active_queues.json", {})
            if kit_key in active_queues:
                return ("exists", active_queues[kit_key])
            qdata = {
                "name": kit,
                "opener": str(interaction.user.id),
                "testers": [str(interaction.user.id)],
                "time": time.time() * 1000,
            }
            active_queues[kit_key] = qdata
            tx.set("active_queues.json", active_queues)
            return ("ok", qdata)

        status, qdata = await transaction(("active_queues.json",), _open)
        if status == "exists":
            existing = qdata
            return await interaction.response.send_message(
                f"❌ Pouze jeden tester může přímo inicializovat frontu! "
                f"Queue pro **{existing['name']}** už je otevřená testerem "
                f"<@{existing['opener']}>. Pokud v ní chceš také testovat, "
                f"použij `/queue joinasqueue kit:{kit}`.",
                ephemeral=True,
            )

        channel_id = get_queue_channel_id(kit_key)
        if not channel_id:
            return await interaction.response.send_message(
                f"❌ Neznámý kit: {kit}", ephemeral=True
            )

        # Panel vždy jde do určeného kanálu daného kitu, ne do kanálu příkazu
        kit_channel = interaction.guild.get_channel(channel_id)
        if kit_channel is None:
            try:
                kit_channel = await interaction.guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                kit_channel = None
        if kit_channel is None:
            return await interaction.response.send_message(
                "❌ Nepodařilo se najít kanál pro tento kit.", ephemeral=True
            )

        # Purge všech zpráv v kanálu kitu před novým panelem (jako v originále)
        try:
            async for message in kit_channel.history(limit=100):
                await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        queue = load_data("queue.json")
        filtered = [p for p in queue if str(p.get("kit", "")).lower() == kit_key]
        embed = create_queue_embed(kit, filtered, qdata["testers"])

        view = QueueView(kit)
        message = await kit_channel.send("📢 @everyone", embed=embed, view=view)

        queue_messages = load_data("queue_messages.json", {})
        queue_messages[kit_key] = {"message_id": str(message.id), "kit": kit}
        save_data("queue_messages.json", queue_messages)

        # Zaregistrování persistentní view pro restart bota
        self.bot.add_view(view, message_id=message.id)

        await interaction.response.send_message(
            f"✅ Fronta pro **{kit}** byla otevřena v <#{channel_id}>!", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /closeq
    # ------------------------------------------------------------------
    @app_commands.command(name="closeq", description="Close a specific kit queue")
    @app_commands.describe(kit="Name of the kit/queue")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def closeq(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Only testers can close queues.", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
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

        # Uzavření = atomicky: smazání aktivní fronty + vyčištění čekajících
        # hráčů kitu + odebrání záznamu panelu (žádné souběžné ztráty zápisu).
        async def _close(tx):
            active = tx.get("active_queues.json", {})
            active.pop(kit_key, None)
            tx.set("active_queues.json", active)

            queue = tx.get("queue.json")
            remaining = [p for p in queue if str(p.get("kit", "")).lower() != kit_key]
            if len(remaining) != len(queue):
                tx.set("queue.json", remaining)

            messages = tx.get("queue_messages.json", {})
            old = messages.pop(kit_key, None)
            tx.set("queue_messages.json", messages)
            return old

        old = await transaction(
            ("active_queues.json", "queue.json", "queue_messages.json"), _close
        )
        old_id = old.get("message_id") if isinstance(old, dict) else old

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

        # Panel se upraví přímo v určeném kanálu kitu (bez tlačítek)
        channel_id = get_queue_channel_id(kit_key)
        kit_channel = None
        if channel_id:
            kit_channel = interaction.guild.get_channel(channel_id)
            if kit_channel is None:
                try:
                    kit_channel = await interaction.guild.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    kit_channel = None

        if kit_channel is not None and old_id:
            try:
                panel_msg = await kit_channel.fetch_message(int(old_id))
                await panel_msg.edit(embed=embed, view=None)
            except (discord.NotFound, discord.HTTPException):
                try:
                    await kit_channel.send(embed=embed)
                except discord.HTTPException:
                    pass
        elif kit_channel is not None:
            try:
                await kit_channel.send(embed=embed)
            except discord.HTTPException:
                pass

        await interaction.response.send_message(
            f"Queue pro **{kit}** has been closed and all players have been removed.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /queue (skupina subpříkazů)
    # ------------------------------------------------------------------
    queue = app_commands.Group(name="queue", description="Manage the testing queue")

    @queue.command(name="join", description="Join the queue for a test")
    @app_commands.describe(ign="Your Minecraft IGN", kit="Kit you want to test")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def queue_join(self, interaction: discord.Interaction, ign: str, kit: str) -> None:
        kit_key = kit.lower()
        user_id = str(interaction.user.id)
        now = time.time() * 1000

        # Společná atomická cesta jako Join tlačítko (JoinModal): aktivní
        # fronta + cooldown + duplicita + zápis v jednom kritickém úseku.
        result = await join_queue(
            user_id,
            interaction.user.name,
            ign,
            kit,
            joined_at_ms=now,
            cooldown_ms=PLAYER_COOLDOWN_MS,
        )
        status = result["result"]

        if status == "closed":
            return await interaction.response.send_message(
                f"❌ Queue pro kit **{kit}** je momentálně zavřená! Počkej, až ji tester otevře.",
                ephemeral=True,
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
        await interaction.response.send_message(f"✅ Byl jsi přidán do fronty **{kit.strip()}**.")

    @queue.command(name="list", description="Zobrazí aktuální fronty a aktivní testery.")
    async def queue_list(self, interaction: discord.Interaction) -> None:
        queue = load_data("queue.json")
        active_queues = load_data("active_queues.json", {})

        description = ""
        if not queue:
            description = "*(Nikdo momentálně nečeká)*\n"
        else:
            for i, p in enumerate(queue, 1):
                ign = p.get("ign")
                ign_part = f" (`{ign}`)" if ign else ""
                description += (
                    f"**{i}.** <@{p.get('id')}>{ign_part} - Kit: **{p.get('kit')}**\n"
                )

        description += "\n**Aktivní fronty a testeři:**\n"
        if not active_queues:
            description += "*(Žádné otevřené fronty)*"
        else:
            for kit_key, data in active_queues.items():
                testers = ", ".join(f"<@{t}>" for t in data.get("testers", []))
                testers = testers or f"<@{data.get('opener')}>"
                description += f"• **{data.get('name')}** | Testeri: {testers}\n"

        embed = discord.Embed(
            title="📋 Přehled front",
            description=description,
            color=0xF59E0B,
            timestamp=discord.utils.utcnow(),
        )
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
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def queue_joinasqueue(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        kit_key = kit.lower()
        user_id = str(interaction.user.id)

        async def _join(tx):
            active_queues = tx.get("active_queues.json", {})
            qdata = active_queues.get(kit_key)
            if not qdata:
                return ("closed", None)
            if user_id in qdata.get("testers", []):
                return ("duplicate", qdata)
            qdata.setdefault("testers", []).append(user_id)
            tx.set("active_queues.json", active_queues)
            return ("ok", qdata)

        status, qdata = await transaction(("active_queues.json",), _join)
        if status == "closed":
            return await interaction.response.send_message(
                "❌ Tato fronta není otevřená!", ephemeral=True
            )
        if status == "duplicate":
            return await interaction.response.send_message(
                "V této frontě už jsi zapsaný jako aktivní tester.", ephemeral=True
            )

        await update_panel(interaction.guild, kit_key)
        await interaction.response.send_message(
            f"⚔️ <@{user_id}> se přidal jako další aktivní tester pro frontu "
            f"**{qdata['name']}**."
        )

    @queue.command(name="leaveq", description="Leave an active queue you are currently testing in")
    @app_commands.describe(kit="Name of the kit")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def queue_leaveq(self, interaction: discord.Interaction, kit: str) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        kit_key = kit.lower()
        user_id = str(interaction.user.id)

        async def _leave(tx):
            active_queues = tx.get("active_queues.json", {})
            qdata = active_queues.get(kit_key)
            if not qdata:
                return ("closed", None)
            testers = qdata.get("testers", [])
            if user_id not in testers:
                return ("not_listed", qdata)
            testers.remove(user_id)

            # Pokud odchází otevíratel, převezme frontu první tester
            if qdata.get("opener") == user_id and testers:
                qdata["opener"] = testers[0]

            tx.set("active_queues.json", active_queues)
            return ("ok", qdata)

        status, qdata = await transaction(("active_queues.json",), _leave)
        if status == "closed":
            return await interaction.response.send_message(
                "Tato fronta neexistuje nebo není aktivní.", ephemeral=True
            )
        if status == "not_listed":
            return await interaction.response.send_message(
                "V této frontě nejsi zapsaný.", ephemeral=True
            )

        await update_panel(interaction.guild, kit_key)
        await interaction.response.send_message(
            f"👋 <@{user_id}> opustil frontu **{qdata['name']}**. "
            "Ostatní testeři mohou pokračovat."
        )

    @queue.command(name="pull", description="Automatically pull the first player")
    async def queue_pull(self, interaction: discord.Interaction) -> None:
        # Stejná kontrola jako pull tlačítko na panelu (views.py) – jen role
        # Tester. Globální "joinastester" registrace není vyžadována.
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )

        queue = load_data("queue.json")
        if not queue:
            return await interaction.response.send_message("Fronta je prázdná.", ephemeral=True)

        # Vezmeme prvního hráče, ale z fronty ho vyřadíme až po výběru roomky
        player = queue[0]
        ign = player.get("ign")
        ign_part = f" (`{ign}`)" if ign else ""
        kit_key = str(player.get("kit", "")).lower()
        active_queues = load_data("active_queues.json", {})
        kit_name = active_queues.get(kit_key, {}).get("name") or player.get("kit", "?")

        # Stejný tok jako pull tlačítko na panelu: tester vybere roomku,
        # hráč do ní dostane přístup a pošle se uvítací zpráva. Vyřazení
        # z fronty je atomické (PullChannelSelectView.on_select).
        view = PullChannelSelectView(player, kit_name)
        await interaction.response.send_message(
            content=(
                f"⚔️ <@{player['id']}>{ign_part} je vytažen z fronty pro "
                f"**{kit_name}**. Vyber roomku, do které ho přidat:"
            ),
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /removeq
    # ------------------------------------------------------------------
    @app_commands.command(name="removeq", description="Manually remove a specific player from the queue")
    @app_commands.describe(hrac="The player you want to remove")
    async def removeq(self, interaction: discord.Interaction, hrac: discord.User) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Chybí oprávnění.", ephemeral=True)

        user_id = str(hrac.id)

        async def _remove(tx):
            queue = tx.get("queue.json")
            entry = next((p for p in queue if p.get("id") == user_id), None)
            if entry is None:
                return None
            tx.set("queue.json", [p for p in queue if p.get("id") != user_id])
            return entry

        entry = await transaction(("queue.json",), _remove)
        if entry is None:
            return await interaction.response.send_message(
                f"❌ Hráč <@{user_id}> nebyl nalezen v žádné aktivní frontě.", ephemeral=True
            )

        await interaction.response.send_message(
            f"🧹 Hráč <@{user_id}> byl vyhozen z fronty pro kit **{entry.get('kit')}**."
        )
        await update_panel(interaction.guild, str(entry.get("kit", "")).lower())

    # ------------------------------------------------------------------
    # /mktesterroom – vytvoří soukromou tester roomku (text kanál)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="mktesterroom",
        description="Vytvoří soukromou tester roomku pro pullnutí hráče",
    )
    @app_commands.describe(
        hrac="Hráč, kterému rovnou nastavit přístup (volitelné)",
        kategorie="Kategorie roomky (volitelné; default z env TESTER_ROOM_CATEGORY_ID)",
    )
    async def mktesterroom(
        self,
        interaction: discord.Interaction,
        hrac: discord.Member = None,
        kategorie: discord.CategoryChannel = None,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Jen testeři můžou vytvářet tester roomky.", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        guild = interaction.guild
        everyone = guild.default_role

        # Kategorie: předaná parametrem, jinak z env, jinak žádná
        category = kategorie
        category_id = TESTER_ROOM_CATEGORY_ID
        if category is None and category_id:
            category = guild.get_channel(category_id)
            if category is None:
                try:
                    category = await guild.fetch_channel(category_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    category = None
            if not isinstance(category, discord.CategoryChannel):
                category = None

        base_name = hrac.display_name if hrac else interaction.user.display_name
        room_name = re.sub(r"[^a-z0-9]+", "-", base_name.lower()).strip("-")[:32]
        room_name = room_name or "tester-room"

        overwrites = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            ),
        }
        player_named = hrac is not None and hrac.id != interaction.user.id
        if player_named:
            overwrites[hrac] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )

        try:
            channel = await guild.create_text_channel(
                name=room_name,
                category=category,
                overwrites=overwrites,
                reason="Tester roomka (/mktesterroom)",
            )
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message(
                "❌ Nepodařilo se vytvořit roomku. Zkontroluj oprávnění bota "
                "(Manage Channels).",
                ephemeral=True,
            )

        room_msg = f"🔒 Tester roomka – vytvořil <@{interaction.user.id}>"
        if player_named:
            room_msg += f"\n👤 Hráč s přístupem: <@{hrac.id}>"
            # Zaznamenání přednastaveného hráče – /result mu pak práva odebere,
            # /skip si odsud přečte záznam. (kit zatím neznáme – zjistí se
            # z fronty, když hráče přepulluje pull tlačítko.)
            await save_pulled_player(
                {
                    "id": str(hrac.id),
                    "username": hrac.display_name,
                    "ign": hrac.display_name,
                    "kit": "",
                    "joinedAt": 0,
                },
                channel.id,
            )
        room_msg += (
            "\nHráč získá přístup po pullnutí (tlačítko Pull Player ⚔️) a po"
            " `/result` mu bude odebrán. Roomku smažeš tlačítkem níže."
        )
        try:
            await channel.send(content=room_msg, view=TesterRoomView())
        except (discord.Forbidden, discord.HTTPException):
            pass

        reply = f"✅ Tester roomka vytvořena: <#{channel.id}>"
        reply += (
            f" – <@{hrac.id}> má přístup." if player_named
            else " – přístup získá hráč po pullnutí."
        )
        await interaction.response.send_message(reply, ephemeral=True)

    # ------------------------------------------------------------------
    # /skip – přeskočí AFK hráče vytáhnutého z fronty
    # ------------------------------------------------------------------
    @app_commands.command(
        name="skip",
        description="Skipne AFK hráče (pullnutého z fronty) – vrátí ho na konec fronty",
    )
    @app_commands.describe(hrac="AFK hráč, kterého přeskočit")
    async def skip(self, interaction: discord.Interaction, hrac: discord.Member) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Na tohle musíš být Tester!", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        player_id = str(hrac.id)
        kit_key = ""
        stored_player = None
        was_pulled = False
        access_revoked = False
        next_player = None

        # 1) Vše atomicky: odebrání práv z roomky + smazání záznamu
        #    (pulled_players.json) + přesun hráče na konec fronty. Dvě souběžné
        #    interakce si navzájem nemůžou ztratit zápis.
        async def _skip_tx(tx):
            nonlocal kit_key, stored_player, was_pulled, access_revoked, next_player

            pulled = tx.get("pulled_players.json", {})
            entry = pulled.get(player_id)
            channel_id_raw = None
            if entry is not None:
                was_pulled = True
                if isinstance(entry, dict):
                    channel_id_raw = entry.get("channel")
                    stored_player = entry.get("player")
                    if isinstance(stored_player, dict):
                        kit_key = str(stored_player.get("kit", "")).lower()
                else:
                    # starší formát záznamu (string = channel id)
                    channel_id_raw = entry
                if channel_id_raw:
                    channel = interaction.guild.get_channel(int(channel_id_raw))
                    if channel is None:
                        try:
                            channel = await interaction.guild.fetch_channel(int(channel_id_raw))
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            channel = None
                    if channel is not None:
                        try:
                            await channel.set_permissions(hrac, overwrite=None)
                            access_revoked = True
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                del pulled[player_id]
                tx.set("pulled_players.json", pulled)

            queue = tx.get("queue.json")
            new_queue, new_kit_key, moved = _move_to_queue_end(queue, stored_player, player_id)
            if not kit_key:
                kit_key = new_kit_key
            requeued = len(new_queue) != len(queue)
            if requeued:
                tx.set("queue.json", new_queue)
            next_player = next(
                (p for p in new_queue if str(p.get("kit", "")).lower() == kit_key), None
            )
            return requeued, moved

        requeued, moved = await transaction(
            ("pulled_players.json", "queue.json"), _skip_tx
        )

        # 1b) Voice: skipnutý hráč nesmí zůstat připojený ve voice roomce
        #     (odebrání práv ho z voice kanálu samo neodpojí).
        try:
            vs = hrac.voice
        except (AttributeError, TypeError):
            vs = None
        if vs is not None and vs.channel is not None:
            try:
                await hrac.move_to(interaction.guild.afk_channel)
            except (discord.Forbidden, discord.HTTPException) as err:
                log.warning(
                    "Nelze odpojit hráče %s z voice roomky po /skip: %s",
                    player_id,
                    err,
                )

        if requeued and kit_key:
            await update_panel(interaction.guild, kit_key)

        if not requeued and not moved and not was_pulled:
            return await interaction.response.send_message(
                f"❌ Hráč **{hrac.display_name}** není pullnutý ani ve frontě.",
                ephemeral=True,
            )

        # 3) Kdo bude další na řadě?
        parts = [f"⏭️ **{hrac.display_name}** byl přeskočen (AFK)."]
        if access_revoked:
            parts.append("• Přístup do roomky mu byl odebrán.")
        if requeued:
            parts.append("• Vrácen na konec fronty – ostatní jdou před ním.")
        if next_player:
            nick = next_player.get("ign") or next_player.get("username") or "?"
            parts.append(
                f"➡️ Další na řadě: **{nick}** (<@{next_player.get('id')}>)"
            )
        else:
            parts.append("📭 Fronta pro tento kit je prázdná.")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Queues(bot))
"""Hlavní vstupní bod bota DACHSHUNDTIERS (Python port).

Spuštění:
    pip install -r requirements.txt
    export DISCORD_TOKEN=...
    python bot.py
"""

import asyncio
import logging
import time

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, GUILD_ID
from storage import ensure_data_dir, load_data
from views import HT3PanelView, HTTicketView, QueueView, TournamentSignupView

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("dachshundtiers")


class DachshundTiersTree(discord.app_commands.CommandTree):
    """Fallback error handler pro VŠECHNY aplikace příkazů.

    Lokální (cog-level ``cog_app_command_error``) handlery běží dál – tenhle
    zachytí vše, co nikdo neošetřil, zaloguje to a hráči pošle hlášku místo
    tichého selhání interakce.
    """

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        log.exception("Chyba v příkazu %s: %s", interaction.command, error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze odeslat chybovou hlášku: %s", err)


class DachshundTiersBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True  # jako v originále

        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=DachshundTiersTree,
            allowed_mentions=discord.AllowedMentions(
                everyone=True, users=True, roles=True
            ),
        )

    async def setup_hook(self) -> None:
        # Načtení cogů
        extensions = [
            "cogs.queues",
            "cogs.results",
            "cogs.ht3",
            "cogs.tournaments",
            "cogs.kits",
            "cogs.roles",
            "cogs.playersync",
            "cogs.websync",
            "cogs.checkweb",
            "cogs.topresult",
            "cogs.datacheck",
            "cogs.info",
        ]
        for extension in extensions:
            try:
                await self.load_extension(extension)
                log.info("Načten cog %s", extension)
            except Exception as err:  # noqa: BLE001
                log.error("Chyba při načítání cogy %s: %s", extension, err)

    async def on_ready(self) -> None:
        log.info("Bot %s (ID: %s) je online!", self.user, self.user.id)
        try:
            from cogs.info import git_commit

            log.info("Commit běžícího bota: %s", git_commit())
        except Exception:  # noqa: BLE001
            pass

        # Registrace slash příkazů (globálně, nebo jen v GUILD_ID).
        # POZOR: pro sync do guildy je nejdřív nutné zkopírovat globální příkazy
        # přes tree.copy_global_to(guild=...). Bez toho discord.py pošle prázdný
        # seznam (PUT []) a smaže slash příkazy daného servru ("Synchronizováno 0").
        target = discord.Object(id=GUILD_ID) if GUILD_ID else None
        try:
            if target is not None:
                self.tree.copy_global_to(guild=target)
            synced = await self.tree.sync(guild=target)
            log.info("Synchronizováno %d slash příkazů", len(synced))
        except Exception as err:  # noqa: BLE001
            log.error("Chyba při synchronizaci příkazů: %s", err)

        # Znovuzaregistrování persistentních view pro živé panely front
        queue_messages = load_data("queue_messages.json", {})
        for kit_key, entry in queue_messages.items():
            message_id = entry.get("message_id") if isinstance(entry, dict) else entry
            kit = entry.get("kit", kit_key) if isinstance(entry, dict) else kit_key
            if not message_id:
                continue
            try:
                self.add_view(QueueView(kit), message_id=int(message_id))
            except (ValueError, discord.ClientException) as err:
                log.warning("Nelze zaregistrovat panel %s: %s", kit_key, err)

        # HT3 panel
        ht3_panel = load_data("ht3_panel_message.json", {})
        if ht3_panel.get("message_id"):
            try:
                self.add_view(HT3PanelView(), message_id=int(ht3_panel["message_id"]))
            except (ValueError, discord.ClientException) as err:
                log.warning("Nelze zaregistrovat HT3 panel: %s", err)

        # HT tickety: re-registrace persistentních tlačítek otevřených ticketů
        # (Claim HT / Unclaim / Close / Reopen). Zavřené tickety už view nepotřebují.
        ht_tickets = load_data("ht_tickets.json", {})
        for channel_id, ticket in ht_tickets.items():
            if not isinstance(ticket, dict):
                continue
            if ticket.get("status") != "open":
                continue
            message_id = ticket.get("panelMessageId")
            if not message_id:
                log.warning(
                    "Ticket %s nemá panelMessageId – tlačítka se neregistrují "
                    "(otevře se znovu po restartu příště).",
                    channel_id,
                )
                continue
            try:
                self.add_view(HTTicketView(), message_id=int(message_id))
            except (ValueError, discord.ClientException) as err:
                log.warning("Nelze zaregistrovat view ticketu %s: %s", channel_id, err)

        # Turnaje: zaregistrování tlačítek a naplánování konce přihlašování
        from cogs.tournaments import end_tournament_signup

        tournaments = load_data("tournaments.json", {})
        now_ms = time.time() * 1000
        for kit_key, tdata in tournaments.items():
            if tdata.get("signupMessageId"):
                try:
                    self.add_view(
                        TournamentSignupView(kit_key),
                        message_id=int(tdata["signupMessageId"]),
                    )
                except (ValueError, discord.ClientException) as err:
                    log.warning("Nelze zaregistrovat turnaj %s: %s", kit_key, err)

            if not tdata.get("ended") and tdata.get("deadline") and tdata.get("guildId"):
                remaining_s = max(0.0, (tdata["deadline"] - now_ms) / 1000.0)
                log.info("Turnaj %s: plánuji ukončení za %d s", kit_key, remaining_s)

                async def _auto_end(guild_id: int, key: str, delay: float) -> None:
                    await asyncio.sleep(delay)
                    guild = self.get_guild(guild_id)
                    if guild is None:
                        try:
                            guild = await self.fetch_guild(guild_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            guild = None
                    if guild is not None:
                        await end_tournament_signup(guild, key)

                asyncio.create_task(
                    _auto_end(int(tdata["guildId"]), kit_key, remaining_s)
                )


async def main() -> None:
    ensure_data_dir()
    bot = DachshundTiersBot()
    await bot.start(DISCORD_TOKEN)


def run() -> None:
    """Spustí bota s retry při přechodných chybách gateway."""
    if not DISCORD_TOKEN:
        raise SystemExit(
            "❌ Chybí DISCORD_TOKEN. Nastav proměnnou prostředí (viz .env.example)."
        )

    # Původní bot neměl vycházet z provozu při přechodných chybách gateway
    # (WebSocket 503 / timeout handshaku), ale u neplatného tokenu se má zastavit.
    while True:
        try:
            asyncio.run(main())
            break
        except (discord.LoginFailure, discord.PrivilegedIntentsRequired) as err:
            log.error("Nepřekonatelná chyba autentizace/konfigurace: %s", err)
            break
        except (
            discord.ConnectionClosed,
            discord.GatewayNotFound,
            discord.HTTPException,
            OSError,
            asyncio.TimeoutError,
        ) as err:
            log.warning("Přechodná chyba gateway (%s). Retry za 5 s...", err)
            time.sleep(5)


if __name__ == "__main__":
    run()
"""Hlavní vstupní bod bota DACHSHUNDTIERS (Python port).

Spuštění:
    pip install -r requirements.txt
    export DISCORD_TOKEN=...
    python bot.py
"""

import asyncio
import logging
import time
from typing import Optional

import discord
from discord.ext import commands

from config import DISCORD_TOKEN, GUILD_ID
from services.config_store import get_ht3_panel
from services.kit_catalog import get_kits
from storage import backend_name, ensure_data_dir
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


def _intended_command_names(tree) -> set:
    """Názvy kořenových příkazů, které má aplikace aktuálně v tree (intended set)."""
    return {c.name for c in tree.get_commands()}


async def _remove_obsolete_guild_commands(
    tree, *, guild: discord.Object, intended: set
) -> list:
    """Odstraní z cílového guildu POUZE obsolete guild příkazy TÉTO aplikace.

    Přejmenované / v kódu odstraněné příkazy z dřívějších nasazení by v guildu
    jinak zůstaly napořád. Smažou se jen ty, jejichž jméno není v intended setu:
      * jen příkazy TÉTO aplikace (fetch/delete je na API scoped na aplikaci),
      * jen v tomto jednom guildu,
      * globální scope se nedotýká (žádné globální mazání/PUT),
      * cizí aplikace se nedotýká (Discord to scopingem vylučuje).

    Jednotlivá selhání jen zalogujeme – finální ``tree.sync(guild=...)`` (PUT)
    přepíše celý set aplikace v guildu a slouží jako bezpečnostní síť.
    """
    app_id = getattr(getattr(tree, "client", None), "application_id", None)
    http = getattr(tree, "_http", None)

    try:
        fetched = await tree.fetch_commands(guild=guild)
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze načíst guild příkazy (migrace): %s", err)
        return []

    removed: list = []
    for cmd in fetched:
        if cmd.name in intended:
            continue
        if app_id is None or http is None:
            # Nedá se smazat jednotlivě – dorovná to finální PUT po sync.
            continue
        try:
            await http.delete_guild_command(app_id, guild.id, cmd.id)
            removed.append(cmd.name)
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning("Nelze smazat obsolete guild příkaz %s: %s", cmd.name, err)
            removed.append(cmd.name)
    return removed


async def sync_commands(
    tree, *, guild_id: Optional[int] = None
) -> dict:
    """Jediná, deterministická a idempotentní synchronizace slash příkazů.

    Synchronizuje se PŘESNĚ JEDEN scope (nikdy oba):

    * ``guild_id`` nastaven – POUZE guild scope (produkce na jednom serveru):
      nejdřív se vyčistí obsolete guild příkazy téhle aplikace (viz
      ``_remove_obsolete_guild_commands``), pak se globální příkazy zkopírují
      do guildy a nasyncují. Globální scope se NIKDY nedotýká → duplicity
      global+guild nevznikají.
    * ``guild_id`` None – POUZE globální scope (default/dev): ``tree.sync()``.
      Guildy se NIKDY nedotýká.

    PUT bulk overwrite je přirozeně idempotentní: opakovaný start nevytvoří
    duplicity. Volající by nicméně měl stejné volání spustit jen jednou
    (viz ``DachshundTiersBot._sync_commands_once``).
    """
    if guild_id is None:
        synced = await tree.sync()
        return {"scope": "global", "synced": len(synced), "removed_guild": []}

    guild = discord.Object(id=guild_id)
    intended = _intended_command_names(tree)
    removed = await _remove_obsolete_guild_commands(
        tree, guild=guild, intended=intended
    )

    tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)
    return {
        "scope": "guild",
        "guild_id": guild_id,
        "synced": len(synced),
        "removed_guild": removed,
    }


def _build_intents() -> discord.Intents:
    """Intenty bota pro celý Discord server.

    ``members`` je PRIVILEGOVANÝ intent – musí být zapnutý i v Developer
    Portálu aplikace (Bot → Privileged Gateway Intents). Bez něj
    ``guild.get_member()`` vrací None (prázdná cache členů), což rozbíjí
    synchronizaci rolí a lookupy členů napříč cogami.
    """
    intents = discord.Intents.default()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True  # jako v originále
    intents.members = True  # privileged – zapnout v portálu (viz README)
    return intents


class DachshundTiersBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=_build_intents(),
            tree_cls=DachshundTiersTree,
            allowed_mentions=discord.AllowedMentions(
                everyone=True, users=True, roles=True
            ),
        )
        # Synchronizace příkazů běží přesně jednou za běh procesu (viz
        # ``_sync_commands_once``); lock chrání před souběžnými on_ready.
        self._commands_synced = False
        self._sync_lock = asyncio.Lock()
        # Startup validace kit-role mapování (design §7) – jednou za běh.
        self._kit_role_config_validated = False

        # PostgreSQL pool (Phase C): engine + session factory žijí po celý běh
        # procesu; cog příkazy (např. /sync discord) z nich berou transakce.
        # ``None`` = PostgreSQL není nakonfigurováno → DB příkazy hlásí tvrdou
        # chybu (žádný tichý fallback na JSON).
        self.db_engine = None
        self.db_session_factory = None

    async def setup_hook(self) -> None:
        # Načtení cogů
        extensions = [
            "cogs.queues",
            "cogs.results",
            "cogs.ht3",
            "cogs.tournaments",
            "cogs.kits",
            "cogs.roles",
            "cogs.sync",
            "cogs.topresult",
            "cogs.info",
            "cogs.edituser",
            "cogs.link",
        ]
        for extension in extensions:
            try:
                await self.load_extension(extension)
                log.info("Načten cog %s", extension)
            except Exception as err:  # noqa: BLE001
                log.error("Chyba při načítání cogy %s: %s", extension, err)

    async def close(self) -> None:
        """Zavře client i PostgreSQL pool; disposuje se až po client close."""
        await super().close()
        if self.db_engine is not None:
            engine, self.db_engine = self.db_engine, None
            try:
                from db.engine import dispose_engine

                await dispose_engine(engine)
            except Exception as err:  # noqa: BLE001 – ukončení nesmí spadnout
                log.warning("Chyba při ukončení PostgreSQL poolu: %s", err)

    async def _sync_commands_once(self) -> None:
        """Spustí synchronizaci příkazů PŘESNĚ JEDNOU za běh procesu.

        ``on_ready`` může proběhnout víckrát (reconnect, přihlášení k více
        guildům) – bez guardu by se každý reconnect zbytečně přepisoval celý
        command set. Samotná synchronizace je přitom deterministická a
        idempotentní (viz ``sync_commands``), takže žádné duplicity nevznikají.

        Race conditions, které tohle řešení potlačuje:
        - souběžné ``on_ready`` události by spustily sync dvakrát (asyncio.Lock
          + dvojitá kontrola flagu),
        - selhání synchronizace (rate limit, odpojení) NESMÍ označit sync za
          „hotový" – flag se nastaví až po úspěchu, takže se sync zkusí znovu
          při dalším ``on_ready`` místo tichého provozu bez příkazů.
        """
        if self._commands_synced:
            return
        async with self._sync_lock:
            if self._commands_synced:
                return
            try:
                info = await sync_commands(self.tree, guild_id=GUILD_ID)
                self._commands_synced = True
                extra = ""
                if info["scope"] == "guild":
                    removed = ", ".join(info["removed_guild"]) or "žádné"
                    extra = (
                        f" [guild {info['guild_id']}, "
                        f"odstraněno {len(info['removed_guild'])} obsolete: {removed}]"
                    )
                log.info(
                    "Synchronizováno %d slash příkazů [scope=%s]%s",
                    info["synced"],
                    info["scope"],
                    extra,
                )
            except Exception as err:  # noqa: BLE001
                log.error("Chyba při synchronizaci příkazů: %s", err)

    async def _validate_kit_role_configuration_once(self) -> None:
        """Startup validace kit-role mapování (design §7, fail-fast).

        Běží jednou za běh procesu, PO přihlášení (potřebuje role guildu).
        Bez PostgreSQL se přeskočí – boot check ``validate_configured_role_ids``
        už proběhl v ``_init_database``; s chybnou konfigurací rolí bot NIKDY
        nepokračuje do sync/reconcile ("If required role configuration is
        missing: FAIL FAST").
        """
        if self._kit_role_config_validated:
            return
        if self.db_session_factory is None:
            self._kit_role_config_validated = True
            return
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.warning(
                "Guilda %s není načtená – validace rolí se zkusí při dalším "
                "on_ready.",
                GUILD_ID,
            )
            return

        from db.config import strict_kit_roles_enabled
        from db.services.config_validation import (
            KitRoleConfigError,
            validate_kit_role_configuration,
        )
        from db.services.session import transaction

        strict = strict_kit_roles_enabled()
        try:
            async with transaction(self.db_session_factory) as session:
                result = await validate_kit_role_configuration(
                    session,
                    guild_role_ids={r.id for r in guild.roles},
                    strict_kits=strict,
                )
        except KitRoleConfigError as err:
            log.error(
                "Konfigurace Discord rolí je neplatná – bot se zastavuje: %s",
                err,
            )
            await self.close()
            raise SystemExit(1) from err
        for warning in result.warnings:
            log.warning("%s", warning)
        log.info(
            "Ověřeno %d mapování kit-tier→Discord role (strict_kits=%s).",
            result.mapping_count,
            strict,
        )
        self._kit_role_config_validated = True

    async def on_ready(self) -> None:
        log.info("Bot %s (ID: %s) je online!", self.user, self.user.id)
        try:
            from cogs.info import git_commit

            log.info("Commit běžícího bota: %s", git_commit())
        except Exception:  # noqa: BLE001
            pass

        # Registrace slash příkazů do JEDINÉHO scope (deterministicky, 1× za běh).
        # Scope: GUILD_ID nastaven → jen guild (produkce); jinak → jen globální.
        await self._sync_commands_once()

        # Startup validace kit-role mapování (fail-fast, design §7) – 1× za běh.
        await self._validate_kit_role_configuration_once()

        # Znovuzaregistrování persistentních view pro živé panely front a
        # otevřené HT tickety. Stav se čte z PostgreSQL (F10) – queue_messages.json
        # ani ht_tickets.json se už nečtou; bez DB se bloky přeskočí (žádný
        # JSON fallback).
        if self.db_session_factory is not None:
            from db.repositories.kits import KitRepository
            from db.repositories.queues import QueueRepository
            from db.repositories.tickets import TicketRepository
            from db.services.session import transaction

            # Aktivní fronty → QueueView panely (Queue.panel_message_id)
            async with transaction(self.db_session_factory) as session:
                queues = await QueueRepository().list_active(session)
                kits = {k.id: k for k in await KitRepository().list(session)}
            for queue in queues:
                kit = kits.get(queue.kit_id)
                if kit is None or not queue.panel_message_id:
                    continue
                try:
                    self.add_view(QueueView(kit.key), message_id=int(queue.panel_message_id))
                except (ValueError, discord.ClientException) as err:
                    log.warning("Nelze zaregistrovat panel %s: %s", kit.key, err)

            # Otevřené HT tickety → persistentní tlačítka (Claim / Unclaim /
            # Close / Reopen). Zavřené tickety už view nepotřebují.
            async with transaction(self.db_session_factory) as session:
                open_tickets = await TicketRepository().list_open(session)
            for ticket in open_tickets:
                if not ticket.panel_message_id:
                    log.warning(
                        "Ticket %s nemá panelMessageId – tlačítka se neregistrují "
                        "(otevře se znovu po restartu příště).",
                        ticket.channel_id,
                    )
                    continue
                try:
                    self.add_view(HTTicketView(), message_id=int(ticket.panel_message_id))
                except (ValueError, discord.ClientException) as err:
                    log.warning(
                        "Nelze zaregistrovat view ticketu %s: %s",
                        ticket.channel_id,
                        err,
                    )

        # HT3 panel
        ht3_panel = await get_ht3_panel(session_factory=self.db_session_factory)
        if ht3_panel.get("message_id"):
            try:
                kits = await get_kits(session_factory=self.db_session_factory)
                self.add_view(
                    HT3PanelView(kits=kits), message_id=int(ht3_panel["message_id"])
                )
            except (ValueError, discord.ClientException) as err:
                log.warning("Nelze zaregistrovat HT3 panel: %s", err)

        # Turnaje: zaregistrování tlačítek a naplánování konce přihlašování.
        # Fáze F (F2/F3): stav se čte z PostgreSQL (tournaments.json se už
        # nezapisuje); bez DB se tento blok přeskočí – žádný JSON fallback.
        if self.db_session_factory is not None:
            from cogs.tournaments import end_tournament_signup
            from db.repositories.kits import KitRepository
            from db.repositories.tournaments import TournamentRepository
            from db.services.session import transaction

            now_ts = time.time()
            resolved: list[tuple] = []
            async with transaction(self.db_session_factory) as session:
                tourneys = await TournamentRepository().list_all(session)
                for tournament in tourneys:
                    kit = await KitRepository().get_by_id(
                        session, tournament.kit_id
                    )
                    if kit is not None:
                        resolved.append((tournament, kit.key))

            for tournament, kit_key in resolved:
                if tournament.signup_message_id:
                    try:
                        self.add_view(
                            TournamentSignupView(kit_key),
                            message_id=int(tournament.signup_message_id),
                        )
                    except (ValueError, discord.ClientException) as err:
                        log.warning("Nelze zaregistrovat turnaj %s: %s", kit_key, err)

                if (
                    not tournament.ended
                    and tournament.deadline is not None
                    and tournament.guild_id
                ):
                    remaining_s = max(
                        0.0, tournament.deadline.timestamp() - now_ts
                    )
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
                            await end_tournament_signup(
                                self.db_session_factory, guild, key
                            )

                    asyncio.create_task(
                        _auto_end(
                            int(tournament.guild_id), kit_key, remaining_s
                        )
                    )

        # Fáze E: observe-only reconciliation (startup jednou + hodinový timer).
        # Nikdy nemění Discord role; jen drainuje potvrzené wedges a zrcadlí
        # aktuální Discord role do PostgreSQL. Bez DB se tiše přeskočí.
        if self.db_session_factory is not None:
            await self._run_reconciliation_once()
            asyncio.create_task(self._reconciliation_loop())

    async def _run_reconciliation_once(self) -> None:
        from cogs._shared import guild_members
        from db.services.reconciliation import ReconciliationService

        guild = self.get_guild(GUILD_ID)
        if guild is None:
            try:
                guild = await self.fetch_guild(GUILD_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Reconciliation: guild %s nedostupný – přeskočeno", GUILD_ID)
                return
        try:
            members = await guild_members(guild)
            outcome = await ReconciliationService().reconcile(
                self.db_session_factory, members=members
            )
            log.info(
                "Reconciliation: %d members, %d mirror ops, %d anomalies, "
                "%d outbox events",
                outcome.sync.scanned_members,
                outcome.sync.observations_applied,
                outcome.sync.anomalies,
                len(outcome.outbox_consumed),
            )
        except Exception:  # noqa: BLE001 – jedna reconciliaci nesmí shodit bota
            log.exception("Reconciliation selhala (zůstává observe-only)")

    async def _reconciliation_loop(self, interval: float = 3600.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._run_reconciliation_once()


async def _init_database():
    """Vytvoří PostgreSQL engine + session factory a vrátí je.

    PostgreSQL je od tohoto refactoru POVINNÝ — žádný JSON-only deployment
    mode ani fallback. Chybějící ``DATABASE_URL`` je vždy tvrdá chyba, stejně
    jako nedostupné/nemigrované PostgreSQL (žádné tajemství v logu/hláškách —
    ani URL, ani heslo). Vrací ``(engine, session_factory)`` nebo nikdy
    nevrátí nic (``SystemExit``). Pool předává ``main()`` botu (``db_engine``
    / ``db_session_factory``) a disposuje se v ``close()``.
    """
    import db.config as dbconfig
    from db.engine import create_async_engine_from_url, make_session_factory
    from db.validation import validate_configured_role_ids, validate_database

    validate_configured_role_ids()

    url = dbconfig.database_url()
    if not url:
        raise SystemExit(
            "❌ Chybí DATABASE_URL (nebo DB_HOST/DB_NAME/DB_USER/DB_PASSWORD). "
            "PostgreSQL je povinný – nastav připojovací údaje (viz .env.example)."
        )

    engine = create_async_engine_from_url(dbconfig.build_async_database_url(url))
    try:
        info = await validate_database(engine)
        log.info("PostgreSQL připraveno (schéma %s).", info["schema_revision"])
    except dbconfig.DatabaseConfigError:
        await engine.dispose()
        raise SystemExit(
            "❌ PostgreSQL selhalo při startovní kontrole. Viz log; oprav "
            "připojení nebo spusť `alembic upgrade head`."
        )
    session_factory = make_session_factory(engine)
    return engine, session_factory


async def main() -> None:
    ensure_data_dir()
    log.info("Úložiště dat: %s", backend_name())
    engine, session_factory = await _init_database()
    bot = DachshundTiersBot()
    bot.db_engine, bot.db_session_factory = engine, session_factory
    log.info("PostgreSQL pool připraven pro cog příkazy.")
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

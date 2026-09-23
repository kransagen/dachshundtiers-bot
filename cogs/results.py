"""Cog s výsledky tier testů a statistikami testerů.

- /result            – zápis výsledku testu (cooldown, players.json, statistiky, GitHub)
- /testerstats       – portfolio jednoho testera
- /testersstats      – tabulka testerů (tento měsíc / všechny časy)
- /addtest           – admin: přidání historických testů
- /removetest        – admin: odečtení testů (upraví total i aktuální měsíc, min 0)
- /removeplayertiers – admin: smazání hráče z players.json
"""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import github_sync
from config import GITHUB_TOKEN, get_result_channel_id
from panel import update_panel
from services.queue_service import leave_queue, remove_pulled_player
from services.store import transaction
from storage import load_data
from utils import (
    add_kit,
    get_kits,
    has_admin_role,
    has_tester_role,
    kit_autocomplete,
    month_key,
    now_ms,
    set_eval,
    today_cz,
)

log = logging.getLogger("dachshundtiers")

# Povolené tiery v /result (HT3 a výš se řeší přes HT3+ tickety):
#   LT5, HT5, LT4, HT4, LT3, LT3 + eval
RESULT_TIERS = {"LT5", "HT5", "LT4", "HT4", "LT3", "LT3E"}
# Volba „LT3 + eval" (v kódu LT3E): hráč dostane tier LT3 (stejná role)
# do players.json a navíc status evalu (data/evals.json) – viz set_eval.
EVAL_TIER = "LT3E"

# Autocomplete tieru pro /result: (zobrazované jméno, přenášená hodnota).
# „LT3 + eval" se přenáší jako LT3E (= EVAL_TIER), ať zůstane normalizace
# v /result stejná (tier.strip().upper()).
TIER_OPTIONS = [
    ("LT5", "LT5"),
    ("HT5", "HT5"),
    ("LT4", "LT4"),
    ("HT4", "HT4"),
    ("LT3", "LT3"),
    ("LT3 + eval", EVAL_TIER),
]


async def tier_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete tierů /result (LT5 .. LT3 + eval), filtruje podle psaní."""
    text = (current or "").lower()
    out = []
    for name, value in TIER_OPTIONS:
        if not text or text in name.lower() or text in value.lower():
            out.append(app_commands.Choice(name=name, value=value))
    return out


async def _log_tester_stat(tester_id: str, kit: str, tier: str, month: str) -> None:
    """Zaloguje statistiky testera (total, kits, tiers, monthly, hourlyLogs).

    Probíhá transakčně – dvě souběžné interakce si nemůžou navzájem přepsat
    statistiku (ztráta testu).
    """
    async def _run(tx):
        stats_db = tx.get("testers_stats.json", {})
        stat = stats_db.setdefault(
            tester_id,
            {"total": 0, "lastTested": "", "kits": {}, "tiers": {}, "monthly": {}, "hourlyLogs": []},
        )
        stat["total"] = stat.get("total", 0) + 1
        stat["lastTested"] = today_cz()
        stat["kits"][kit] = stat["kits"].get(kit, 0) + 1
        stat["tiers"][tier] = stat["tiers"].get(tier, 0) + 1
        stat["monthly"][month] = stat["monthly"].get(month, 0) + 1
        stat["hourlyLogs"].append(time.localtime().tm_hour)
        tx.set("testers_stats.json", stats_db)

    return await transaction(("testers_stats.json",), _run)


# ---------------------------------------------------------------------------
# GitHub synchronizace players.json (ekvivalent původní Octokit integrace)
# ---------------------------------------------------------------------------
def _apply_result_to_players(
    players: list, ign: str, mode: str, new_tier: str, current_date: str
) -> list:
    """Aplikuje výsledek /result na daný seznam hráčů (pro GitHub push).

    Idempotentní vůči libovolnému aktuálnímu seznamu – při konfliktu (409) ji
    ``github_sync.push_players`` zavolá znovu na čerstvě stažených datech.
    """
    players = [dict(p) for p in (players or [])]
    player = next(
        (p for p in players if p.get("username", "").lower() == ign.lower()), None
    )
    if player is None:
        player = {"username": ign, "modes": {}, "history": {}}
        players.append(player)

    player.setdefault("modes", {})
    player.setdefault("history", {})
    player["history"].setdefault(mode, [])
    player["modes"][mode] = new_tier
    player["history"][mode].append({"date": current_date, "tier": new_tier})
    return players


async def _sync_players_github_async(interaction, ign, mode, new_tier, current_date) -> None:
    try:
        ok, message, _ = await github_sync.push_players(
            f"Update {ign} - {mode}: {new_tier}",
            lambda players: _apply_result_to_players(players, ign, mode, new_tier, current_date),
            success_message="✅ Úspěšně aktualizováno na GitHubu!",
        )
        if ok:
            await interaction.followup.send(
                f"{message}\n- **Hráč:** {ign}\n- **Mód:** {mode}\n- **Tier:** {new_tier}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(message, ephemeral=True)
    except Exception as err:  # noqa: BLE001
        await interaction.followup.send(
            f"❌ Nastala chyba při zápisu na GitHub: {err}", ephemeral=True
        )


class Results(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        """Zachytí neošetřené chyby příkazů – pošle hlášku a zaloguje traceback."""
        log.exception("Chyba v příkazu %s: %s", interaction.command, error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # /result
    # ------------------------------------------------------------------
    @app_commands.command(name="result", description="Submit a test result")
    @app_commands.describe(
        hrac="The tested Discord player",
        ign="Minecraft IGN of the player",
        kit="The kit tested",
        tier="New achieved tier",
        score="Score of the match (e.g. 5-2)",
        outcome="Tester outcome",
        add_role="Role, kterou hráči přidat (nepovinné)",
        remove_role="Role, kterou hráči odebrat (nepovinné)",
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="Tester Won", value="Won"),
            app_commands.Choice(name="Tester Lost", value="Lost"),
        ]
    )
    @app_commands.autocomplete(kit=kit_autocomplete, tier=tier_autocomplete)
    async def result(
        self,
        interaction: discord.Interaction,
        hrac: discord.User,
        ign: str,
        kit: str,
        tier: str,
        score: str,
        outcome: str,
        add_role: discord.Role = None,
        remove_role: discord.Role = None,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        # Défer hned napřed – stejně jako originál (index.ts: deferReply),
        # abychom se vešli do 3s okna Discord i přes fetch roomky / kanálu.
        await interaction.response.defer(ephemeral=True)

        target_id = str(hrac.id)
        ign_clean = ign.strip()
        kit_clean = kit.strip()
        kit_key = kit_clean.lower()
        tier_up = tier.strip().upper()

        # 0a) Validace tieru: v /result jdou zadat jen tiery do LT3 + eval
        #     (HT3 a výš se teď řeší výhradně přes HT3+ tickety).
        if tier_up not in RESULT_TIERS:
            return await interaction.followup.send(
                "❌ Neplatný tier! V `/result` lze zadat pouze: "
                "**LT5, HT5, LT4, HT4, LT3, LT3 + eval**.",
                ephemeral=True,
            )

        # 0b) „LT3 + eval" (LT3E) = tier LT3 (stejná role) + eval status.
        #     Do players.json / webu / role jde „LT3", eval se uloží zvlášť.
        is_eval = tier_up == EVAL_TIER
        stored_tier = "LT3" if is_eval else tier_up  # players.json + web + role
        display_tier = "LT3 + eval" if is_eval else tier_up  # embed / texty

        # 0) Auto-registrace nového kitu (jako /addkit) – aby se hned objevil
        #    v autocomplete /result, HT3+ panelu a u turnajů.
        new_kit_added = False
        if not any(existing.lower() == kit_key for existing in get_kits()):
            new_kit_added = add_kit(kit_clean)
            if new_kit_added:
                try:
                    from cogs.kits import _refresh_ht3_panel

                    await _refresh_ht3_panel(self.bot)
                except Exception:  # noqa: BLE001
                    pass

        # 1) Cooldown hráče (4 dny) – transakčně, aby se souběžné /result
        #    navzájem nepřepsaly.
        async def _set_cooldown(tx):
            cooldowns = tx.get("cooldowns.json", {})
            cooldowns[target_id] = now_ms()
            tx.set("cooldowns.json", cooldowns)

        await transaction(("cooldowns.json",), _set_cooldown)

        # 2) Odebrání z fronty (atomické – viz services.queue_service)
        removed_from_queue = await leave_queue(target_id, kit_key)
        if removed_from_queue and interaction.guild is not None:
            await update_panel(interaction.guild, kit_key)

        # 3) Odebrání práv z tester roomek – po výsledku hráč nesmí zůstat
        #    v žádné roomce. Pokrývá pull tlačítko / /queue pull (záznam ve
        #    pulled_players.json) i přednastavený přístup přes `/mktesterroom
        #    hrac:` – vždy odstraníme hráčův osobní overwrite ve VŠECH
        #    kanálech serveru.
        await remove_pulled_player(target_id)

        member = interaction.guild.get_member(int(target_id))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(target_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        if member is not None:
            removed_count = 0
            for ch in interaction.guild.channels:
                try:
                    has_member_ow = any(
                        isinstance(t, discord.Member) and t.id == member.id
                        for t in ch.permission_overwrites
                    )
                except (AttributeError, TypeError):
                    continue
                if not has_member_ow:
                    continue
                try:
                    await ch.set_permissions(member, overwrite=None)
                    removed_count += 1
                except (discord.Forbidden, discord.HTTPException) as err:
                    log.warning(
                        "Nelze odebrat práva hráče %s v kanálu %s: %s",
                        target_id,
                        ch.id,
                        err,
                    )
            if removed_count:
                log.info(
                    "Po /result odebrána práva hráče %s v %d kanálu(ech)",
                    target_id,
                    removed_count,
                )

            # Voice roomky: odebrání práv z voice kanálu hráče automaticky
            # NEodpojí – kdo je zrovna připojený, zůstane viset v roomce.
            # Po výsledku ho proto přesuneme do AFK kanálu (nebo odpojíme).
            try:
                vs = member.voice
            except (AttributeError, TypeError):
                vs = None
            if vs is not None and vs.channel is not None:
                try:
                    await member.move_to(interaction.guild.afk_channel)
                except (discord.Forbidden, discord.HTTPException) as err:
                    log.warning(
                        "Nelze odpojit hráče %s z voice roomky po /result: %s",
                        target_id,
                        err,
                    )

        # 4) Statistiky testera (transakčně)
        month = month_key()
        current_date = today_cz()
        await _log_tester_stat(str(interaction.user.id), kit_clean, display_tier, month)

        # 5) players.json – transakčně (dvě souběžné /result si nemůžou
        #    navzájem přepsat zápis hráčů).
        async def _update_players(tx):
            players = tx.get("players.json")
            db_player = next(
                (p for p in players if p.get("username", "").lower() == ign_clean.lower()), None
            )
            prev = "N/A"

            if db_player is None:
                db_player = {"username": ign_clean, "modes": {}, "history": {}}
                db_player["modes"][kit_clean] = stored_tier
                db_player["history"][kit_clean] = [{"date": current_date, "tier": stored_tier}]
                players.append(db_player)
            else:
                db_player.setdefault("modes", {})
                db_player.setdefault("history", {})
                db_player["history"].setdefault(kit_clean, [])
                if db_player["modes"].get(kit_clean):
                    prev = db_player["modes"][kit_clean].upper()
                db_player["modes"][kit_clean] = stored_tier
                db_player["history"][kit_clean].append(
                    {"date": current_date, "tier": stored_tier}
                )
            tx.set("players.json", players)
            return prev

        previous_tier = await transaction(("players.json",), _update_players)

        # 5a) „LT3 + eval" → status evalu (data/evals.json). Tier/role zůstávají
        #     LT3 – hráč ale nově může otevírat HT3+ tickety.
        eval_note = ""
        if is_eval and set_eval(ign_clean, kit_clean):
            eval_note = (
                f"\n🎖️ **{ign_clean}** dostal **LT3 + eval** pro **{kit_clean}** – "
                "může otevírat HT3+ tickety."
            )

        # 5b) Volitelné role (add_role / remove_role) – jako v originále
        #     (aplikuje se tiše, nezobrazuje se v embedu výsledku)
        if add_role is not None or remove_role is not None:
            member = interaction.guild.get_member(int(target_id))
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(int(target_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is not None:
                try:
                    if add_role is not None:
                        await member.add_roles(add_role)
                    if remove_role is not None:
                        await member.remove_roles(remove_role)
                except (discord.Forbidden, discord.HTTPException) as err:
                    log.warning(
                        "Nelze upravit role hráče %s: %s", target_id, err
                    )

        # 5c) Automatická role kitu+tieru (data/kit_roles.json) – po uložení
        #     výsledku dostane hráč roli nového tieru, staré tiery kitu se
        #     odeberou. Poznámka se připojí k potvrzení.
        role_note = ""
        try:
            from cogs.roles import auto_grant_kit_role

            role_note = await auto_grant_kit_role(
                interaction.guild, target_id, kit_key, stored_tier
            )
        except Exception:  # noqa: BLE001
            log.exception("Chyba při automatickém udělování role pro %s", target_id)

        # 6) Embed s výsledkem
        avatar_url = f"https://minotar.net/armor/bust/{ign_clean}/100.png"
        embed = (
            discord.Embed(
                title=f"📝 Výsledek tier testu – {kit_clean.lower()}",
                description=f"Tier test byl úspěšně dokončen pro frontu **{kit_clean.lower()}**!",
                color=0x10B981,
            )
            .set_thumbnail(url=avatar_url)
            .add_field(name="👤 Hráč (Discord)", value=f"<@{target_id}>", inline=True)
            .add_field(name="🎮 Minecraft IGN", value=f"`{ign_clean}`", inline=True)
            .add_field(name="⚔️ Tester", value=f"<@{interaction.user.id}>", inline=True)
            .add_field(
                name="📊 Skóre / Výsledek",
                value=f"`{score}` ({'Tester vyhrál' if outcome == 'Won' else 'Tester prohrál'})",
                inline=False,
            )
            .add_field(name="📉 Předchozí tier", value=f"`{previous_tier}`", inline=True)
            .add_field(name="📈 Nový tier", value=f"**{display_tier}**", inline=True)
            .set_footer(text=current_date)
        )

        # 7) Odeslání výsledku do určeného kanálu podle tieru (jako v originále)
        result_channel_id = get_result_channel_id(stored_tier)
        result_channel = self.bot.get_channel(result_channel_id)
        if result_channel is None and interaction.guild is not None:
            try:
                result_channel = await interaction.guild.fetch_channel(result_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                result_channel = None

        content = (
            f"📢 **Nový výsledek pro {kit_clean}!** | "
            f"Hráč: <@{target_id}> | Tester: <@{interaction.user.id}>"
        )
        saved_msg = (
            f"✅ Výsledek uložen na GitHubu pro hráče **{ign_clean}** — "
            f"mód **{kit_clean}**, tier **{display_tier}**."
        )
        if new_kit_added:
            saved_msg += (
                f"\n🎉 Nový kit **{kit_clean}** byl automaticky zaregistrován "
                "do data/kits.json (autocomplete, HT3+ panel, turnaje)."
            )
        if eval_note:
            saved_msg += eval_note
        if role_note:
            saved_msg += role_note

        if result_channel is not None:
            try:
                await result_channel.send(content=content, embed=embed)
                await interaction.followup.send(
                    f"{saved_msg} Odesláno do <#{result_channel_id}>.",
                    ephemeral=True,
                )
            except (discord.Forbidden, discord.HTTPException) as err:
                log.warning("Nelze poslat do výsledkového kanálu %s: %s", result_channel_id, err)
                await interaction.followup.send(
                    saved_msg, embed=embed, ephemeral=True
                )
        else:
            await interaction.followup.send(
                f"{saved_msg} (Výsledkový kanál <#{result_channel_id}> nebyl nalezen.)",
                embed=embed,
                ephemeral=True,
            )

        # 8) Volitelný GitHub sync
        if GITHUB_TOKEN:
            asyncio.create_task(
                _sync_players_github_async(interaction, ign_clean, kit_clean, stored_tier, current_date)
            )

    # ------------------------------------------------------------------
    # /testerstats
    # ------------------------------------------------------------------
    @app_commands.command(name="testerstats", description="View detailed stats for a specific tester")
    @app_commands.describe(tester="Select the tester")
    async def testerstats(self, interaction: discord.Interaction, tester: discord.User = None) -> None:
        target = tester or interaction.user
        stats_db = load_data("testers_stats.json", {})
        tdata = stats_db.get(str(target.id))

        if not tdata or tdata.get("total", 0) == 0:
            return await interaction.response.send_message(
                f"❌ Uživatel <@{target.id}> nemá žádné uložené statistiky testů."
            )

        kits = tdata.get("kits", {})
        tiers = tdata.get("tiers", {})
        fav_kit = max(kits, key=kits.get) if kits else "Žádný"
        top_tier = max(tiers, key=tiers.get) if tiers else "Žádný"

        hourly = tdata.get("hourlyLogs", [])
        avg_hour = round(sum(hourly) / len(hourly)) if hourly else 0

        embed = (
            discord.Embed(
                title=f"⚔️ Portfolio Testera – {target.name}",
                color=0x3B82F6,
                timestamp=discord.utils.utcnow(),
            )
            .add_field(name="📈 Celkem testů", value=f"`{tdata.get('total', 0)}`", inline=True)
            .add_field(
                name="📅 Naposledy testoval",
                value=f"`{tdata.get('lastTested') or 'Nikdy'}`",
                inline=True,
            )
            .add_field(name="🎮 Nejoblíbenější Kit", value=f"`{fav_kit}`", inline=True)
            .add_field(name="🏆 Nejčastěji dávaný Tier", value=f"`{top_tier}`", inline=True)
            .add_field(name="⏰ Průměrný čas testu", value=f"`Kolem {avg_hour}:00 hod`", inline=True)
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /testersstats
    # ------------------------------------------------------------------
    @app_commands.command(name="testersstats", description="Zobrazí tabulku testerů.")
    @app_commands.describe(period="Filtruj podle měsíce nebo celkově")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Tento měsíc", value="current"),
            app_commands.Choice(name="Všechny časy", value="all"),
        ]
    )
    async def testersstats(self, interaction: discord.Interaction, period: str) -> None:
        stats_db = load_data("testers_stats.json", {})
        current_month = month_key()

        entries: list = []
        for tester_id, data in stats_db.items():
            if period == "all":
                score = data.get("total", 0)
            else:
                score = data.get("monthly", {}).get(current_month, 0)
            if score > 0:
                entries.append((tester_id, score))

        entries.sort(key=lambda item: item[1], reverse=True)
        top10 = entries[:10]

        if top10:
            description = "\n".join(
                f"**{i + 1}.** <@{tester_id}>: {score} testů"
                for i, (tester_id, score) in enumerate(top10)
            )
        else:
            description = "Žádné testy pro toto období."

        embed = discord.Embed(
            title=(
                "🏆 TOP testeři všechny časy"
                if period == "all"
                else "🏆 TOP testeři tento měsíc"
            ),
            color=0x9B59B6,
            description=description,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /addtest (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="addtest", description="Admin command to manually add historical test logs")
    @app_commands.describe(
        tester="The tester to credit",
        amount="Amount of tests to add",
        month="Month format (MM.YYYY, e.g. 06.2026)",
    )
    async def addtest(
        self, interaction: discord.Interaction, tester: discord.User, amount: int, month: str
    ) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        tester_id = str(tester.id)

        async def _run(tx):
            stats_db = tx.get("testers_stats.json", {})
            stat = stats_db.setdefault(
                tester_id,
                {"total": 0, "lastTested": "", "kits": {}, "tiers": {}, "monthly": {}, "hourlyLogs": []},
            )
            stat["total"] = stat.get("total", 0) + amount
            stat["monthly"][month] = stat["monthly"].get(month, 0) + amount
            tx.set("testers_stats.json", stats_db)

        await transaction(("testers_stats.json",), _run)

        await interaction.response.send_message(
            f"✅ Úspěšně přidáno **{amount}** historických testů uživateli "
            f"<@{tester_id}> na měsíc **{month}**."
        )

    # ------------------------------------------------------------------
    # /removetest (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="removetest", description="Odečte testy testerovi")
    @app_commands.describe(
        user="Tester, kterému chceš odebrat testy", amount="Počet testů k odebrání"
    )
    async def removetest(self, interaction: discord.Interaction, user: discord.User, amount: int) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        tester_id = str(user.id)
        updated_total = 0

        # Upraví celkový součet i aktuální měsíční počet (nikdy pod nulu)
        async def _run(tx):
            nonlocal updated_total
            stats_db = tx.get("testers_stats.json", {})
            stat = stats_db.get(tester_id)
            if not stat:
                stat = {
                    "total": 0,
                    "lastTested": "",
                    "kits": {},
                    "tiers": {},
                    "monthly": {},
                    "hourlyLogs": [],
                }
                stats_db[tester_id] = stat
            stat["total"] = max(0, stat.get("total", 0) - amount)
            stat["monthly"][month_key()] = max(
                0, stat["monthly"].get(month_key(), 0) - amount
            )
            updated_total = stat["total"]
            tx.set("testers_stats.json", stats_db)

        await transaction(("testers_stats.json",), _run)

        await interaction.response.send_message(
            f"📉 Uživatel **{user.name}** ztratil **{amount}** test(ů). "
            f"Nyní má celkem **{updated_total}** testů."
        )

    # ------------------------------------------------------------------
    # /removeplayertiers (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="removeplayertiers", description="Smaže všechny tiery hráče na webu")
    @app_commands.describe(ign="Minecraft jméno hráče (IGN)")
    async def removeplayertiers(self, interaction: discord.Interaction, ign: str) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        async def _run(tx):
            players = tx.get("players.json")
            index = next(
                (i for i, p in enumerate(players) if p.get("username", "").lower() == ign.lower()),
                None,
            )
            if index is None:
                return False
            players.pop(index)
            tx.set("players.json", players)
            return True

        removed = await transaction(("players.json",), _run)
        if not removed:
            return await interaction.response.send_message(
                f"Hráč **{ign}** nebyl v databázi nalezen.", ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Hráči **{ign}** byly úspěšně smazány všechny tiery a byl odebrán z webu."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Results(bot))
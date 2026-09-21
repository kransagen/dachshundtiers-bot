"""Cog s výsledky tier testů a statistikami testerů.

- /result            – zápis výsledku testu (cooldown, players.json, statistiky, GitHub)
- /testerstats       – portfolio jednoho testera
- /testersstats      – tabulka testerů (tento měsíc / všechny časy)
- /addtest           – admin: přidání historických testů
- /removetest        – admin: odečtení testů (upraví total i aktuální měsíc, min 0)
- /removeplayertiers – admin: smazání hráče z players.json
"""

import asyncio
import base64
import json
import time

import discord
import requests
from discord import app_commands
from discord.ext import commands

from config import GITHUB_FILE_PATH, GITHUB_OWNER, GITHUB_REPO, GITHUB_TOKEN
from panel import update_panel
from storage import load_data, save_data
from utils import DEFAULT_KITS, get_kits, has_tester_role, month_key, now_ms, today_cz


async def kit_autocomplete(
    interaction: discord.Interaction, current: str
):
    """Autocomplete názvů kitů pro /result (z data/kits.json)."""
    kits = get_kits() or list(DEFAULT_KITS)
    if current:
        kits = [k for k in kits if current.lower() in k.lower()]
    return [app_commands.Choice(name=k, value=k) for k in kits[:25]]


def _log_tester_stat(stats_db, tester_id: str, kit: str, tier: str, month: str) -> None:
    """Zaloguje statistiky testera (total, kits, tiers, monthly, hourlyLogs)."""
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
    save_data("testers_stats.json", stats_db)


# ---------------------------------------------------------------------------
# GitHub synchronizace players.json (ekvivalent původní Octokit integrace)
# ---------------------------------------------------------------------------
def _sync_players_github(ign: str, mode: str, new_tier: str, current_date: str):
    api = (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/contents/{GITHUB_FILE_PATH}"
    )
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    response = requests.get(api, headers=headers, timeout=20)
    sha = None
    if response.status_code == 200:
        data = response.json()
        sha = data.get("sha")
        try:
            players = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            players = []
    elif response.status_code == 404:
        players = []
    else:
        return False, f"GitHub GET selhal ({response.status_code})"

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

    body = {
        "message": f"Update {ign} - {mode}: {new_tier}",
        "content": base64.b64encode(
            json.dumps(players, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        body["sha"] = sha

    response = requests.put(api, headers=headers, json=body, timeout=20)
    if response.status_code in (200, 201):
        return True, "✅ Úspěšně aktualizováno na GitHubu!"
    return False, f"❌ GitHub zápis selhal ({response.status_code})"


async def _sync_players_github_async(interaction, ign, mode, new_tier, current_date) -> None:
    try:
        ok, message = await asyncio.to_thread(
            _sync_players_github, ign, mode, new_tier, current_date
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
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="Tester Won", value="Won"),
            app_commands.Choice(name="Tester Lost", value="Lost"),
        ]
    )
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def result(
        self,
        interaction: discord.Interaction,
        hrac: discord.User,
        ign: str,
        kit: str,
        tier: str,
        score: str,
        outcome: str,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        target_id = str(hrac.id)
        ign_clean = ign.strip()
        kit_clean = kit.strip()
        kit_key = kit_clean.lower()
        tier_up = tier.strip().upper()

        # 1) Cooldown hráče (4 dny)
        cooldowns = load_data("cooldowns.json", {})
        cooldowns[target_id] = now_ms()
        save_data("cooldowns.json", cooldowns)

        # 2) Odebrání z fronty
        queue = load_data("queue.json")
        new_queue = [
            p
            for p in queue
            if not (p.get("id") == target_id and str(p.get("kit", "")).lower() == kit_key)
        ]
        if len(new_queue) != len(queue):
            save_data("queue.json", new_queue)
            await update_panel(interaction.channel, kit_key)

        # 3) Odebrání práv z roomky (pokud byl hráč vytažen)
        pulled_players = load_data("pulled_players.json", {})
        room_id = pulled_players.pop(target_id, None)
        if room_id:
            save_data("pulled_players.json", pulled_players)
            try:
                room = interaction.guild.get_channel(int(room_id))
                if room is None:
                    room = await interaction.guild.fetch_channel(int(room_id))
                member = interaction.guild.get_member(int(target_id))
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(int(target_id))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None
                if room is not None and member is not None:
                    await room.set_permissions(member, overwrite=None)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

        # Odebrání práv i v aktuální roomce (jako v původním botovi)
        if interaction.channel is not None:
            try:
                await interaction.channel.set_permissions(
                    interaction.guild.get_member(int(target_id)) or hrac, overwrite=None
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # 4) Statistiky testera
        month = month_key()
        current_date = today_cz()
        stats_db = load_data("testers_stats.json", {})
        _log_tester_stat(stats_db, str(interaction.user.id), kit_clean, tier_up, month)

        # 5) players.json
        players = load_data("players.json")
        db_player = next(
            (p for p in players if p.get("username", "").lower() == ign_clean.lower()), None
        )
        previous_tier = "N/A"

        if db_player is None:
            db_player = {"username": ign_clean, "modes": {}, "history": {}}
            db_player["modes"][kit_clean] = tier_up
            db_player["history"][kit_clean] = [{"date": current_date, "tier": tier_up}]
            players.append(db_player)
        else:
            db_player.setdefault("modes", {})
            db_player.setdefault("history", {})
            db_player["history"].setdefault(kit_clean, [])
            if db_player["modes"].get(kit_clean):
                previous_tier = db_player["modes"][kit_clean].upper()
            db_player["modes"][kit_clean] = tier_up
            db_player["history"][kit_clean].append({"date": current_date, "tier": tier_up})
        save_data("players.json", players)

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
            .add_field(name="📈 Nový tier", value=f"**{tier_up}**", inline=True)
            .set_footer(text=current_date)
        )

        await interaction.response.send_message(
            f"📢 **Nový výsledek pro {kit_clean}!**", embed=embed
        )

        # 7) Volitelný GitHub sync
        if GITHUB_TOKEN:
            asyncio.create_task(
                _sync_players_github_async(interaction, ign_clean, kit_clean, tier_up, current_date)
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
            discord.Embed(title=f"⚔️ Portfolio Testera – {target.name}", color=0x3B82F6)
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
    @app_commands.command(name="testersstats", description="View the tester leaderboard table")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Current Month", value="current"),
            app_commands.Choice(name="All Time", value="all"),
        ]
    )
    async def testersstats(self, interaction: discord.Interaction, period: str) -> None:
        stats_db = load_data("testers_stats.json", {})
        current_month = month_key()

        leaderboard = []
        for tester_id, data in stats_db.items():
            if period == "all":
                score = data.get("total", 0)
            else:
                score = data.get("monthly", {}).get(current_month, 0)
            if score > 0:
                leaderboard.append((tester_id, score))

        leaderboard.sort(key=lambda item: item[1], reverse=True)

        embed = discord.Embed(
            title=f"🏆 Tabulka Testerů – {'Všechny časy' if period == 'all' else 'Tento měsíc'}",
            color=0xF59E0B,
        )

        if not leaderboard:
            embed.description = "Žádné testy pro toto období."
        else:
            table = "```\nPozice | Tester           | Počet testů\n---------------------------------------\n"
            for index, (tester_id, score) in enumerate(leaderboard, 1):
                user = self.bot.get_user(int(tester_id))
                name = (user.name if user else tester_id).ljust(16)
                table += f"{str(index).ljust(6)} | {name} | {score}\n"
            table += "```"
            embed.description = table

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /addtest (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="addtest", description="Admin command to manually add historical test logs")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        tester="The tester to credit",
        amount="Amount of tests to add",
        month="Month format (MM.YYYY, e.g. 06.2026)",
    )
    async def addtest(
        self, interaction: discord.Interaction, tester: discord.User, amount: int, month: str
    ) -> None:
        stats_db = load_data("testers_stats.json", {})
        tester_id = str(tester.id)
        stat = stats_db.setdefault(
            tester_id,
            {"total": 0, "lastTested": "", "kits": {}, "tiers": {}, "monthly": {}, "hourlyLogs": []},
        )
        stat["total"] = stat.get("total", 0) + amount
        stat["monthly"][month] = stat["monthly"].get(month, 0) + amount
        save_data("testers_stats.json", stats_db)

        await interaction.response.send_message(
            f"✅ Úspěšně přidáno **{amount}** historických testů uživateli "
            f"<@{tester_id}> na měsíc **{month}**."
        )

    # ------------------------------------------------------------------
    # /removetest (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="removetest", description="Odečte testy testerovi")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="Tester, kterému chceš odebrat testy", amount="Počet testů k odebrání"
    )
    async def removetest(self, interaction: discord.Interaction, user: discord.User, amount: int) -> None:
        stats_db = load_data("testers_stats.json", {})
        tester_id = str(user.id)
        stat = stats_db.get(tester_id, {})

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

        # Upraví celkový součet i aktuální měsíční počet (nikdy pod nulu)
        stat["total"] = max(0, stat.get("total", 0) - amount)
        stat["monthly"][month_key()] = max(
            0, stat["monthly"].get(month_key(), 0) - amount
        )
        save_data("testers_stats.json", stats_db)

        await interaction.response.send_message(
            f"📉 Uživatel **{user.name}** ztratil **{amount}** test(ů). "
            f"Nyní má celkem **{stat['total']}** testů."
        )

    # ------------------------------------------------------------------
    # /removeplayertiers (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="removeplayertiers", description="Smaže všechny tiery hráče na webu")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(ign="Minecraft jméno hráče (IGN)")
    async def removeplayertiers(self, interaction: discord.Interaction, ign: str) -> None:
        players = load_data("players.json")
        index = next(
            (i for i, p in enumerate(players) if p.get("username", "").lower() == ign.lower()),
            None,
        )
        if index is None:
            return await interaction.response.send_message(
                f"Hráč **{ign}** nebyl v databázi nalezen.", ephemeral=True
            )

        players.pop(index)
        save_data("players.json", players)
        await interaction.response.send_message(
            f"✅ Hráči **{ign}** byly úspěšně smazány všechny tiery a byl odebrán z webu."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Results(bot))
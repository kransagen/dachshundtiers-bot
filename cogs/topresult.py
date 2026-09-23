"""Žebříček top výsledků – /topresult.

Čte VŽDY z kanonické ``data/players.json`` (jediný zdroj pravdy, žádná druhá
databáze). Řadí podle skutečné tier hierarchie projektu (HT3_TIER_LADDER):
nejlepší tier hráče napříč kity → počet kitů s ním → počet záznamů → jméno.

- ``/topresult``            – celkový žebříček (paginovaný),
- ``/topresult @player``    – konkrétní hráč (umístění, tier testy per kit),
- ``/topresult tier:HT3``   – jen hráči s nejlepším tierem právě HT3,
- ``/topresult limit:15``   – velikost stránky,
- ``/topresult page:2``     – číslo stránky (paginace).

Veřejný příkaz (není admin-only).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.topresult import (
    DEFAULT_LIMIT,
    build_ranking,
    filter_by_tier,
    find_player,
    paginate,
    player_summary,
    tier_display,
)
from storage import load_data

log = logging.getLogger("dachshundtiers")


def _member_names(member) -> list:
    return [
        getattr(member, "display_name", None),
        getattr(member, "name", None),
        getattr(member, "nick", None),
    ]


def _ranking_embed(
    result: dict,
    *,
    limit: int,
    page: int,
    total_pages: int,
    tier_filter: str = None,
) -> discord.Embed:
    items = result["items"]
    if tier_filter:
        title = f"🏆 Top výsledky – tier {tier_display(tier_filter)}"
        desc_pre = f"Hráči s nejlepším tierem **{tier_display(tier_filter)}**."
    else:
        title = "🏆 Top výsledky"
        desc_pre = "Nejlepší tier napříč kity (hierarchie LT5 … HT1)."
    embed = discord.Embed(
        title=title,
        description=f"{desc_pre}\nZdroj: kanonická `players.json` "
        f"(celkem **{result['total']}** hráčů).",
        color=0xF59E0B,
    )
    if not items:
        embed.add_field(name="Žádní hráči", value="Na této stránce nikdo není.", inline=False)
    else:
        lines = [
            f"`#{e['rank']:>3}` **{e['username']}** — "
            f"**{e['best_tier_display']}** ×{e['best_kits_count']} · "
            f"{e['total']} záznamů"
            for e in items
        ]
        embed.add_field(
            name=f"Umístění ({result['start'] + 1}–{result['end']} z {result['total']})",
            value="\n".join(lines),
            inline=False,
        )
    embed.set_footer(
        text=f"Strana {page}/{total_pages} · limit {limit} "
        f"· řazení: nejlepší tier → počet kitů → počet záznamů"
    )
    return embed


def _player_embed(summary: dict, member) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏆 Top výsledky – {summary['username']}",
        description=(
            f"Celkové umístění: **#{summary['rank']}** – nejlepší tier "
            f"**{summary['best_tier_display']}** ({summary['total']} záznamů)."
            if summary["rank"] is not None
            else (
                f"Hráč má jen tiery mimo žebříček ({summary['total']} záznamů) – "
                "do pořadí se nepočítá."
            )
        ),
        color=0xF59E0B,
    )
    if summary["kits"]:
        rows = [f"`{kit}` — **{tier}**" for kit, tier in summary["kits"]]
        embed.add_field(name="Tier testy per kit", value="\n".join(rows), inline=False)
    else:
        embed.add_field(name="Tier testy per kit", value="Žádné záznamy.", inline=False)
    if member is not None:
        embed.set_footer(text=f"Discord: {member.display_name}")
    return embed


class TopResult(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="topresult",
        description="Žebříček top výsledků z kanonické players.json",
    )
    @app_commands.describe(
        player="Hráč (Discord účet) – ukáže jeho umístění a tier testy",
        tier="Filtr: hráči s nejlepším tierem právě tímto tierem (např. HT3)",
        limit="Velikost stránky (1–25)",
        page="Číslo stránky",
    )
    async def topresult(
        self,
        interaction: discord.Interaction,
        player: discord.Member = None,
        tier: str = None,
        limit: app_commands.Range[int, 1, 25] = DEFAULT_LIMIT,
        page: int = 1,
    ) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)

        players = load_data("players.json", []) or []
        ranking = build_ranking(players)
        if not ranking["ranked"] and not ranking["excluded"]:
            return await interaction.followup.send(
                "❌ players.json je prázdná – není co zobrazit.", ephemeral=True
            )

        # ---- @player --------------------------------------------------
        if player is not None:
            found = find_player(players, _member_names(player))
            if found is None:
                return await interaction.followup.send(
                    f"❌ **{player.display_name}** není v players.json "
                    "(není zapsaný žádný tier test).",
                    ephemeral=True,
                )
            summary = player_summary(ranking, found)
            embed = _player_embed(summary, player)
            if ranking["excluded"]:
                embed.add_field(
                    name="Poznámka",
                    value=f"Mimo žebříček: {len(ranking['excluded'])} hráčů "
                    "jen s turnajovými/R-tiery.",
                    inline=False,
                )
            return await interaction.followup.send(embed=embed, ephemeral=True)

        # ---- tier filtr ------------------------------------------------
        tier_filter = None
        entries = ranking["ranked"]
        if tier:
            try:
                entries = filter_by_tier(entries, tier)
            except ValueError as err:
                return await interaction.followup.send(str(err), ephemeral=True)
            tier_filter = tier

        paginated = paginate(entries, limit=limit, page=page)
        embed = _ranking_embed(
            paginated,
            limit=paginated["limit"],
            page=paginated["page"],
            total_pages=paginated["total_pages"],
            tier_filter=tier_filter,
        )
        if ranking["excluded"] and tier_filter is None:
            embed.add_field(
                name="Mimo žebříček",
                value=f"{len(ranking['excluded'])} hráčů má jen tiery mimo "
                "hierarchii (S/A/B turnajové nebo R-tiery) – nerankují se.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TopResult(bot))
"""Kontrola integrity dat – /datacheck.

Projede VŠECHNY místní databáze (kanonická players.json je jediný zdroj
pravdy) a nahlásí problémy:

  - duplicitní hráči / Discord ID / IGN,
  - neplatné tiery (mimo známou hierarchii),
  - konfliktní Discord role (jedna role na víc kitů/tierů),
  - chybějící webové záznamy (modes bez historie),
  - neplatné eval reference, osamocené tickety a výsledky,
  - retired tiery v modes (archivovaná historie) a duplicitní discordId hráčů.

NIC se automaticky nemaže. Když existují **bezpečné opravy** (jen zavření
osamoceného ticketu – záznam zůstává – a bezeztrátová normalizace tierů),
zobrazí se tlačítko ``🔧 Aplikovat bezpečné opravy`` s počtem; aplikace
vyžaduje explicitní potvrzení (tlačítko = potvrzení) a vše se auditlugguje
do ``data/datacheck_log.json``. Jen pro administrátory.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.datacheck import (
    DATACHECK_LOG_FILE,
    KIND_LABELS,
    KINDS,
    perform_repairs,
    run_datacheck,
)
from services.permissions import has_admin_role
from views import SafeView

log = logging.getLogger("dachshundtiers")


def _channel_exists_resolver(guild: discord.Guild):
    def resolver(channel_id: str) -> bool:
        if guild is None:
            return True
        try:
            channel = guild.get_channel(int(channel_id))
        except (TypeError, ValueError):
            return False
        return channel is not None

    return resolver


def _summary_text(summary: dict) -> str:
    return "\n".join(
        f"{KIND_LABELS[k]}: **{summary.get(k, 0)}**" for k in KINDS
    ) or "_žádné nálezy_"


def _report_embed(report: dict) -> discord.Embed:
    if not report["has_issues"]:
        embed = discord.Embed(
            title="✅ /datacheck – vše v pořádku",
            description="Všechny databáze jsou konzistentní – nemám co hlásit.",
            color=0x10B981,
        )
        embed.set_footer(text=f"Audit: data/{DATACHECK_LOG_FILE} (zaznamenáno).")
        return embed

    embed = discord.Embed(
        title="🔍 /datacheck – kontrola integrity",
        description=_summary_text(report["summary"]),
        color=0xEF4444,
    )
    findings = report["findings"]
    lines = [f["message"] for f in findings[:12]]
    if len(findings) > 12:
        lines.append(f"…a dalších {len(findings) - 12} nálezů")
    embed.add_field(
        name=f"Nálezů celkem: {report['total_findings']}",
        value="\n".join(lines) or "_nic_",
        inline=False,
    )

    r = report["repairable"]
    if report["repairable_count"]:
        parts = []
        if r["close_ticket"]:
            parts.append(f"zavřít **{len(r['close_ticket'])}** osamocených ticketů")
        if r["normalize_tier"]:
            parts.append(f"normalizovat **{len(r['normalize_tier'])}** tierů")
        embed.add_field(
            name="🔧 Bezpečné opravy (nic se nemaže)",
            value=(
                f"Po potvrzení tlačítkem: {', '.join(parts)}. "
                "Záznamy zůstávají, audit se zapíše."
            ),
            inline=False,
        )
    embed.set_footer(
        text="Nic se nemění automaticky – opravy jen po potvrzení. "
        f"Audit: data/{DATACHECK_LOG_FILE}."
    )
    return embed


def _repair_result_embed(result: dict) -> discord.Embed:
    ok = bool(result.get("ok"))
    embed = discord.Embed(
        title="🔧 /datacheck – bezpečné opravy",
        description=(
            f"{result['message']}\n"
            f"- zavřeno ticketů: **{len(result['closed'])}** "
            f"(z toho {sum(1 for c in result['closed'] if c.get('skipped'))} už zavřených)\n"
            f"- normalizováno tierů: **{len(result['normalized'])}**\n"
            f"- chyb: **{len(result['errors'])}**"
        ),
        color=0x10B981 if ok else 0xEF4444,
    )
    if result["errors"]:
        embed.add_field(
            name="Chyby",
            value="\n".join(str(e) for e in result["errors"][:15]),
            inline=False,
        )
    embed.set_footer(text=f"Zapsáno do data/{DATACHECK_LOG_FILE} (audit).")
    return embed


class DataCheckRepairView(SafeView):
    """Tlačítko „Aplikovat bezpečné opravy" pro /datacheck."""

    def __init__(self):
        super().__init__(timeout=120)
        self.finished = False

    @discord.ui.button(
        label="🔧 Aplikovat bezpečné opravy",
        style=discord.ButtonStyle.success,
        custom_id="datacheck_repair",
    )
    async def repair(self, interaction: discord.Interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if self.finished:
            return await interaction.response.send_message(
                "✅ Opravy už proběhly.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        self.finished = True

        # Čerstvá kontrola (stav se mohl změnit od náhledu).
        report = await run_datacheck(
            channel_exists=_channel_exists_resolver(guild),
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        r = report["repairable"]
        if not r["close_ticket"] and not r["normalize_tier"]:
            embed = discord.Embed(
                title="✅ /datacheck – už není co opravit",
                description="Kontrola po náhledu nehlásí žádné bezpečné opravy.",
                color=0x10B981,
            )
        else:
            result = await perform_repairs(
                close_ticket_ids=r["close_ticket"],
                tier_fixes=r["normalize_tier"],
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
            )
            embed = _repair_result_embed(result)

        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit zprávu /datacheck: %s", err)
        await interaction.followup.send(embed=embed, ephemeral=True)


class DataCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="datacheck",
        description="Kontrola integrity dat (duplicity, tiery, tickety, výsledky…)",
    )
    async def datacheck(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        report = await run_datacheck(
            channel_exists=_channel_exists_resolver(interaction.guild),
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        embed = _report_embed(report)

        if report["repairable_count"]:
            view = DataCheckRepairView()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DataCheck(bot))
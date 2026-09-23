"""Synchronizace webu s kanonickou hráčskou databází – /websync.

Web (DachshundTiers) čte ``players.json`` z GitHubu. Tento příkaz porovná web
s kanonickou ``data/players.json`` a po potvrzení web nahradí kanonickou DB:

- ``/websync preview`` – ukáže přehled rozdílů, nic neposílá,
- ``/websync apply``   – ukáže přehled a vyžaduje **explicitní potvrzení**
  (tlačítko) před zápisem na web; kanonická DB se mezi náhledem a potvrzením
  ověřuje, aby se nahrálo přesně to, co admin viděl.

Detekuje: chybějící hráče na webu, špatné tiery, zastaralá data, duplicitní
hráče a neplatné záznamy. Po úspěšné synchronizaci se do
``data/websync_log.json`` zapíše timestamp, počet záznamů, úspěch/selhání
a chyby. Obě funkce jsou jen pro administrátory.

Pozn.: Discord API neumožňuje přímé volání skupinového příkazu bez
subpříkazu („/websync" bez parametru není validní) – potvrzovací proud je
proto dostupný jako ``/websync apply``.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.permissions import has_admin_role
from services.websync import (
    KIND_LABELS,
    KINDS,
    fingerprint_canonical,
    preview_website,
    sync_website,
)
from storage import load_data
from views import SafeView

log = logging.getLogger("dachshundtiers")

SYNC_MESSAGE = "websync: synchronizace hráčů na web"


def _summary_text(summary: dict) -> str:
    lines = [f"{KIND_LABELS[k]}: **{summary.get(k, 0)}**" for k in KINDS]
    return "\n".join(lines)


def _websync_embed(result: dict, *, mode: str) -> discord.Embed:
    """Embed s přehledem rozdílů / výsledkem (preview / apply)."""
    if not result["ok"]:
        embed = discord.Embed(
            title="⚠️ /websync – nelze pokračovat",
            description=result["message"],
            color=0xEF4444,
        )
        if mode == "apply":
            embed.set_footer(text="Nic se na web neposílalo.")
        return embed

    analysis = result["analysis"] or {}
    if analysis.get("has_issues"):
        embed = discord.Embed(
            title=f"🔎 /websync – {'potvrzení synchronizace' if mode == 'apply' else 'náhled'}",
            description=(
                f"Kanonická DB: **{analysis['canonical_count']}** hráčů · "
                f"Web: **{analysis['website_count']}** hráčů\n\n"
                + _summary_text(analysis["summary"])
            ),
            color=0xF59E0B,
        )
        lines = [f["message"] for f in analysis["findings"]]
        shown = lines[:15]
        if len(lines) > 15:
            shown.append(f"…a dalších {len(lines) - 15} nálezů")
        embed.add_field(name="Rozdíly oproti webu", value="\n".join(shown), inline=False)

        if analysis.get("website_only"):
            extra = [u for u in analysis["website_only"][:15]]
            note = f"{len(analysis['website_only'])} hráčů"
            embed.add_field(
                name="Hráči jen na webu (ne v kanonické DB)",
                value=(
                    "Synchronizace web přepíše kanonickou DB – tito hráči z webu "
                    f"zmizí: {', '.join(extra)}{'…' if len(analysis['website_only']) > 15 else ''}"
                ),
                inline=False,
            )

        if mode == "apply":
            embed.set_footer(
                text=f"Synchronizace NAHRADÍ players.json na webu kanonickou DB "
                f"({analysis['canonical_count']} záznamů) – potvrď tlačítkem níže."
            )
        else:
            embed.set_footer(text="Náhled – nic se neposílalo. Pro potvrzení použij /websync apply.")
    else:
        embed = discord.Embed(
            title="✅ /websync – web je v synchronizaci",
            description=(
                f"Kanonická DB (**{analysis['canonical_count']}** hráčů) odpovídá "
                "webu – nemám co opravovat."
            ),
            color=0x10B981,
        )
    return embed


class WebSyncConfirmView(SafeView):
    """Tlačítko „Potvrdit a nahrát na web" pro /websync apply."""

    def __init__(self, *, cog, canonical_fingerprint: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.fingerprint = canonical_fingerprint
        self.finished = False

    @discord.ui.button(
        label="✅ Potvrdit a nahrát na web",
        style=discord.ButtonStyle.success,
        custom_id="websync_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button) -> None:
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
                "✅ Synchronizace už proběhla.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # Ověření, že se kanonická DB od náhledu nezměnila – nahrajeme PŘESNĚ
        # to, co admin potvrdil (nikdy nic automaticky navíc).
        canonical = load_data("players.json", []) or []
        if not canonical or fingerprint_canonical(canonical) != self.fingerprint:
            self.finished = True
            await self._finish(interaction, stale=True)
            return

        result = await sync_website(
            canonical=canonical,
            message=SYNC_MESSAGE,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        self.finished = True
        await self._finish(interaction, result=result, stale=False)

    async def _finish(self, interaction, *, result=None, stale=False) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /websync – stav se změnil",
                description=(
                    "Kanonická players.json se mezitím změnila – **nic jsem "
                    "na web neposlal**. Spusť **/websync apply** znovu."
                ),
                color=0xEF4444,
            )
        else:
            ok = bool(result and result["ok"])
            embed = discord.Embed(
                title=(
                    "✅ /websync – web synchronizován"
                    if ok
                    else "❌ /websync – synchronizace selhala"
                ),
                description=result["message"] if result else "",
                color=0x10B981 if ok else 0xEF4444,
            )
            if ok:
                embed.add_field(
                    name="Záznamy na webu",
                    value=f"**{result['records']}** hráčů · "
                    f"{len(result['errors'])} chyb · {result['attempts']} pokusů",
                    inline=False,
                )
            embed.set_footer(text="Zapsáno do data/websync_log.json (audit).")

        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit potvrzovací zprávu: %s", err)
        await interaction.followup.send(embed=embed, ephemeral=True)


class WebSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    websync = app_commands.Group(
        name="websync",
        description="Synchronizace players.json na web (jen pro adminy)",
    )

    # ------------------------------------------------------------------
    # /websync preview
    # ------------------------------------------------------------------
    @websync.command(
        name="preview",
        description="Porovná web s players.json – nic neposílá",
    )
    async def preview(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        canonical = load_data("players.json", []) or []
        result = await preview_website(
            canonical=canonical,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        embed = _websync_embed(result, mode="preview")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /websync apply
    # ------------------------------------------------------------------
    @websync.command(
        name="apply",
        description="Porovná web s players.json a po potvrzení web nahradí kanonickou DB",
    )
    async def apply(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        canonical = load_data("players.json", []) or []

        # Stejný náhled jako /websync preview – ten určuje, co se potvrdí.
        result = await preview_website(
            canonical=canonical,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        embed = _websync_embed(result, mode="apply")

        if not result["ok"]:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not result["analysis"]["has_issues"]:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = WebSyncConfirmView(
            cog=self, canonical_fingerprint=result["fingerprint"]
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WebSync(bot))
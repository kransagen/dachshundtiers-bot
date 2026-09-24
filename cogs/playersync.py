"""Discord tier role synchronizace – /playersync.

Porovnává Discord tier role (``data/kit_roles.json``) s kanonickou databází
hráčů (``data/players.json``). NIKDY neřeší konflikty automaticky:

- ``/playersync preview`` – ukáže přehled rozdílů, nic nemění,
- ``/playersync apply``   – ukáže stejný přehled a vyžaduje **explicitní
  potvrzení** (tlačítko) před aplikací změn; stav se mezi náhledem a
  potvrzením ověřuje, aby se aplikovalo přesně to, co admin viděl.

Každé použití (preview i apply) se zapisuje do ``data/playersync_log.json``
(audit). Obě funkce jsou jen pro administrátory.

Pozn.: Discord API neumožňuje přímé volání skupinového příkazu bez
subpříkazu („/playersync" bez parametru není validní) – proto je potvrzovací
proud dostupný jako ``/playersync apply``.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.permissions import has_admin_role
from services.role_sync import (
    KIND_LABELS,
    KINDS,
    analyze_role_sync,
    fingerprint,
    log_playersync_event,
    make_member,
)
from storage import load_data
from utils import get_kits
from views import SafeView

log = logging.getLogger("dachshundtiers")

KIT_ROLES_FILE = "kit_roles.json"


async def _guild_members(guild: discord.Guild) -> list:
    """Všichni členové serveru bez botů (plný seznam, jinak cache)."""
    members = [m for m in guild.members if not m.bot]
    try:
        fetched = await guild.fetch_members().flatten()
        if fetched:
            members = [m for m in fetched if not m.bot]
    except Exception:  # noqa: BLE001 – bez members intentu fallback na cache
        pass
    return members


def _member_to_dict(member) -> dict:
    return make_member(
        member.id,
        member.display_name,
        {str(r.id) for r in member.roles},
        extra_names=[member.name, member.nick],
    )


def _summary_text(summary: dict) -> str:
    return "\n".join(
        f"{KIND_LABELS[k]}: **{summary.get(k, 0)}**" for k in KINDS
    )


def _playersync_embed(analysis: dict, *, mode: str) -> discord.Embed:
    """Embed s přehledem rozdílů (preview / apply)."""
    if not analysis["findings"]:
        embed = discord.Embed(
            title="✅ /playersync – vše v pořádku",
            description="Role tierů odpovídají players.json. Nemám co opravovat.",
            color=0x10B981,
        )
    else:
        embed = discord.Embed(
            title=f"🔎 /playersync – {'potvrzení změn' if mode == 'apply' else 'náhled'}",
            description=(
                f"Zkontrolováno párů (člen × kit): **{analysis['checked']}** "
                f"(beze změny: **{analysis.get('unchanged', 0)}**)\n\n"
                + _summary_text(analysis["summary"])
            ),
            color=0xF59E0B,
        )
        lines = [f["message"] for f in analysis["findings"]]
        shown = lines[:15]
        if len(lines) > 15:
            shown.append(f"…a dalších {len(lines) - 15} nálezů")
        embed.add_field(name="Zjištěné rozdíly", value="\n".join(shown), inline=False)

    if mode == "apply":
        if analysis["has_actions"]:
            embed.set_footer(
                text=f"Navržených změn: {len(analysis['actions'])} – pro aplikaci "
                "potvrď tlačítkem níže."
            )
        else:
            embed.set_footer(
                text="Žádné změny nelze aplikovat automaticky – viz nálezy "
                "(oprava je ruční)."
            )
    else:
        embed.set_footer(text="Náhled – žádné změny neaplikovány. Pro potvrzení použij /playersync apply.")
    return embed


class PlayerSyncConfirmView(SafeView):
    """Tlačítko „Potvrdit a aplikovat" pro /playersync apply.

    Při potvrzení se stav znovu analyzuje a porovná otisk akcí s tím, co admin
    viděl v náhledu. Liší-li se (něco se mezitím změnilo), nic se neaplikuje.
    """

    def __init__(self, *, cog, analysis: dict):
        super().__init__(timeout=120)
        self.cog = cog
        self.fingerprint = analysis["fingerprint"]
        self.finished = False

    @discord.ui.button(
        label="✅ Potvrdit a aplikovat",
        style=discord.ButtonStyle.success,
        custom_id="playersync_confirm",
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
                "✅ Změny už byly aplikované.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # 1) Ověření, že se stav od náhledu nezměnil → aplikujeme PŘESNĚ to,
        #    co admin potvrdil (nikdy nic automaticky navíc).
        fresh = await self.cog._gather(guild)
        if fingerprint(fresh["actions"]) != self.fingerprint:
            self.finished = True
            await self._finish(interaction, applied=None, stale=True)
            return

        # 2) Aplikace akcí – každá zvlášť, chyby se nikdy nešíří dál.
        applied = await self.cog._apply_actions(guild, fresh["actions"])

        # 3) Audit.
        try:
            await log_playersync_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="apply",
                summary=fresh["summary"],
                applied=applied,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit aplikaci
            log.exception("Auditní zápis (apply) selhal")

        self.finished = True
        await self._finish(interaction, applied=applied, stale=False)

    async def _finish(self, interaction, *, applied, stale) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /playersync – stav se změnil",
                description=(
                    "Mezitím se změnily role nebo players.json – **nic jsem "
                    "neaplikoval**. Spusť **/playersync apply** znovu."
                ),
                color=0xEF4444,
            )
        else:
            ok = sum(1 for a in applied if a.get("ok"))
            lines = []
            for a in applied[:15]:
                mark = "✅" if a.get("ok") else "❌"
                op = "přidána" if a.get("op") == "add" else "odebrána"
                line = f"{mark} <@{a.get('memberId')}> – {op} role <@&{a.get('roleId')}>"
                if not a.get("ok"):
                    line += f" (chyba: {a.get('error')})"
                lines.append(line)
            if len(applied) > 15:
                lines.append(f"…a dalších {len(applied) - 15} akcí")
            embed = discord.Embed(
                title="✅ /playersync – změny aplikovány",
                description=(
                    f"Úspěšně: **{ok} / {len(applied)}** akcí.\n\n" + "\n".join(lines)
                ),
                color=0x10B981 if ok == len(applied) and applied else 0xF59E0B,
            )
            embed.set_footer(text="Zapsáno do data/playersync_log.json (audit).")

        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit potvrzovací zprávu: %s", err)
        await interaction.followup.send(embed=embed, ephemeral=True)


class PlayerSync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    playersync = app_commands.Group(
        name="playersync",
        description="Synchronizace tier rolí s players.json (jen pro adminy)",
    )

    async def _gather(self, guild: discord.Guild) -> dict:
        """Čerstvá analýza: role vs. players.json (nic nemění)."""
        roles_map = load_data(KIT_ROLES_FILE, {}) or {}
        players = load_data("players.json", []) or []
        kit_display = {str(k).lower(): str(k) for k in get_kits()}
        members = [_member_to_dict(m) for m in await _guild_members(guild)]
        return analyze_role_sync(players, members, roles_map, kit_display)

    async def _apply_actions(self, guild: discord.Guild, actions: list) -> list:
        """Aplikuje akce (add/remove rolí); každá akce se vyhodnotí zvlášť."""
        applied = []
        for action in actions:
            member_id = str(action.get("member_id") or "")
            role_id = str(action.get("role_id") or "")
            record = {
                "op": action.get("op"),
                "memberId": member_id,
                "memberName": action.get("member_name") or "",
                "roleId": role_id,
                "kit": action.get("kit") or "",
                "tier": action.get("tier") or "",
                "ok": False,
                "error": None,
            }
            member = None
            if member_id.isdigit():
                member = guild.get_member(int(member_id))
                if member is None:
                    try:
                        member = await guild.fetch_member(int(member_id))
                    except (
                        discord.NotFound,
                        discord.Forbidden,
                        discord.HTTPException,
                    ):
                        member = None
            role = guild.get_role(int(role_id)) if role_id.isdigit() else None
            if member is None:
                record["error"] = "člen není na serveru"
            elif role is None:
                record["error"] = "role neexistuje"
            else:
                try:
                    if action.get("op") == "add":
                        await member.add_roles(role)
                    else:
                        await member.remove_roles(role)
                    record["ok"] = True
                except (discord.Forbidden, discord.HTTPException) as err:
                    record["error"] = str(err)
            applied.append(record)
        return applied

    # ------------------------------------------------------------------
    # /playersync preview
    # ------------------------------------------------------------------
    @playersync.command(
        name="preview",
        description="Porovná tier role s players.json – nic nemění",
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
        analysis = await self._gather(interaction.guild)
        try:
            await log_playersync_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="preview",
                summary=analysis["summary"],
                actions=analysis["actions"],
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit výpis
            log.exception("Auditní zápis (preview) selhal")

        embed = _playersync_embed(analysis, mode="preview")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /playersync apply
    # ------------------------------------------------------------------
    @playersync.command(
        name="apply",
        description="Porovná tier role s players.json a po potvrzení je opraví",
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
        analysis = await self._gather(interaction.guild)
        embed = _playersync_embed(analysis, mode="apply")

        if not analysis["findings"]:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not analysis["has_actions"]:
            embed.set_footer(
                text="Žádné změny nelze aplikovat automaticky – viz nálezy (oprava je ruční)."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = PlayerSyncConfirmView(cog=self, analysis=analysis)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerSync(bot))
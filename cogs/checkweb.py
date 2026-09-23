"""Bezpečná synchronizace – /checkweb (preview / apply), jen pro adminy.

Nová verze /checkweb NIKDY nic nezapisuje automaticky. Původní verze brala
Discord tier role jako autoritativní zdroj a sama přepisovala players.json
i historii – toto chování je zrušené.

- ``/checkweb preview`` – porovná Discord role × players.json × web
  (statusy MATCH / MISSING_DISCORD_ROLE / DATABASE_MISMATCH /
  WEBSITE_MISMATCH / MULTIPLE_TIER_ROLES / UNKNOWN_ROLE /
  UNKNOWN_PLAYER / DUPLICATE_PLAYER). **Nic nemění.**
- ``/checkweb apply``   – ukáže záznamy k řešení a vyžaduje **explicitní
  per-záznamové rozhodnutí** ([Use Discord] / [Keep Database] / [Ignore])
  + potvrzení tlačítkem. JEN tak se smí měnit databáze (a to JEN podle
  Discord rolí, bez zápisu do historie).

Zdroj pravdy zůstává hodnocení (``/result``) – Discord role jsou projekce DB,
ne naopak. Web se tímto příkazem nemění (slouží na to ``/websync``). Každé
použití (i každé rozhodnutí) se zapisuje do ``data/checkweb_log.json``
(audit: actor, player, kit, old tier, new tier, reason, timestamp, source).

Pozn.: Discord API neumožňuje přímé volání skupinového příkazu bez subpříkazu
(„/checkweb" bez parametru není validní, stejně jako u /playersync a /websync)
– proto je jediným zapisovatelem ``/checkweb apply`` s potvrzením.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import github_sync
from services.checkweb import (
    STATUS_LABELS,
    STATUSES,
    apply_checkweb_decisions,
    analyze_checkweb,
    fingerprint_resolvable,
    log_checkweb_event,
)
from services.permissions import has_admin_role
from services.playersync import make_member
from services.store import transaction
from storage import load_data
from utils import get_kits
from views import SafeView

log = logging.getLogger("dachshundtiers")

KIT_ROLES_FILE = "kit_roles.json"

# Kompaktní značky statusů pro přehled
STATUS_MARKERS = {
    "MATCH": "✅",
    "MISSING_DISCORD_ROLE": "➕",
    "DATABASE_MISMATCH": "✏️",
    "WEBSITE_MISMATCH": "🌐",
    "MULTIPLE_TIER_ROLES": "🔁",
    "UNKNOWN_ROLE": "❓",
    "UNKNOWN_PLAYER": "👤",
    "DUPLICATE_PLAYER": "👥",
}


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
        f"{STATUS_LABELS[s]}: **{summary.get(s, 0)}**" for s in STATUSES
    )


def _compact(record: dict) -> str:
    """Jednořádkový přehled záznamu pro náhled."""
    db = record.get("db") or "—"
    dc = ", ".join(record.get("discord") or []) or "—"
    web = record.get("web") or "—"
    marker = STATUS_MARKERS.get(record.get("status"), "•")
    return (
        f"{marker} **{record['player']}** · **{record['kit']}**: "
        f"DB {db} · DC {dc} · WEB {web}"
    )


def _checkweb_embed(analysis: dict, *, mode: str) -> discord.Embed:
    """Embed s přehledem (preview / apply)."""
    records = analysis["records"]
    summary = analysis["summary"]
    source = analysis.get("website_source", "")

    if not records:
        embed = discord.Embed(
            title="✅ /checkweb – vše v pořádku",
            description=(
                "Discord role odpovídají players.json a webu – nemám co "
                f"opravovat.\nWeb: {source}."
            ),
            color=0x10B981,
        )
    else:
        embed = discord.Embed(
            title=f"🔎 /checkweb – {'potvrzení' if mode == 'apply' else 'náhled'}",
            description=(
                f"Zkontrolováno záznamů (hráč × kit): **{analysis['checked']}**\n"
                f"Web: {source}\n\n"
                + _summary_text(summary)
            ),
            color=0xF59E0B,
        )
        lines = [
            _compact(r) for r in records if r["status"] != "MATCH"
        ]
        shown = lines[:12]
        if len(lines) > 12:
            shown.append(f"…a dalších {len(lines) - 12} nálezů")
        embed.add_field(name="Nálezy", value="\n".join(shown), inline=False)
        if summary.get("MATCH"):
            embed.add_field(
                name="✅ Shoda",
                value=f"**{summary['MATCH']}** záznamů odpovídá ve všech zdrojích.",
                inline=False,
            )

    if mode == "apply":
        resolvable = analysis.get("resolvable") or []
        if resolvable:
            embed.set_footer(
                text=f"{len(resolvable)} záznamů k řešení – vyber rozhodnutí "
                "a potvrď tlačítkem. Web se nemění (na to je /websync)."
            )
        else:
            embed.set_footer(
                text="Žádný záznam nevyžaduje rozhodnutí – opravy jsou ruční "
                "(viz nálezy) nebo přes /websync apply."
            )
    else:
        embed.set_footer(
            text="Náhled – žádné změny neaplikovány. JEDINÝ zapisovatel je "
            "/checkweb apply s potvrzením."
        )
    return embed


class CheckWebApplyView(SafeView):
    """Per-záznamová rozhodnutí pro /checkweb apply.

    U KONFLIKTU (víc tier rolí jednoho kitu) se nikdy nevybírá automaticky –
    každá Discord role se nabídne zvlášť ([Use Discord]). Při potvrzení se
    stav znovu analyzuje a porovná otisk s tím, co admin viděl; liší-li se,
    nic se neaplikuje.
    """

    def __init__(self, *, cog, records, fingerprint, decisions=None, page=0):
        super().__init__(timeout=300)
        self.cog = cog
        self.records = records
        self.fingerprint = fingerprint
        self.decisions = decisions or {}
        self.page = page
        self.finished = False

    # ------------------------------------------------------------------
    # Pomocné
    # ------------------------------------------------------------------
    @staticmethod
    def _key(record) -> str:
        return f"{record.get('player')}|{record.get('kit_key')}"

    def _current(self) -> dict:
        return self.records[self.page]

    def _embed(self) -> discord.Embed:
        rec = self._current()
        total = len(self.records)
        decided = len(self.decisions)
        embed = discord.Embed(
            title="🔎 /checkweb apply – rozhodnutí",
            description=(
                f"Záznam **{self.page + 1} / {total}** · rozhodnuto "
                f"**{decided} / {total}**\n\n{rec['message']}"
            ),
            color=0xF59E0B,
        )
        embed.add_field(name="Status", value=rec.get("label", rec["status"]), inline=False)
        decision = self.decisions.get(self._key(rec))
        if decision:
            embed.add_field(name="Rozhodnuto", value=decision.get("label", ""), inline=False)
        embed.set_footer(
            text="„Use Discord“ změní JEN tier v players.json (bez historie). "
            "Web se nemění – na to je /websync."
        )
        return embed

    def _next_view(self) -> "CheckWebApplyView":
        return CheckWebApplyView(
            cog=self.cog,
            records=self.records,
            fingerprint=self.fingerprint,
            decisions=self.decisions,
            page=self.page,
        )

    async def _swap(self, interaction, new_view) -> None:
        """Překreslí zprávu s čerstvým view (stránka / rozhodnutí)."""
        await interaction.response.defer()
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=new_view._embed(), view=new_view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /checkweb: %s", err)

    # ------------------------------------------------------------------
    # Rozhodnutí (select)
    # ------------------------------------------------------------------
    async def on_decision(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await interaction.response.send_message(
                "ℹ️ Vyber rozhodnutí z menu.", ephemeral=True
            )
        rec = self._current()
        key = self._key(rec)
        value = values[0]
        decision, _, tier = value.partition(":")
        label = next(
            (o["label"] for o in rec.get("options", []) if o["key"] == value),
            value,
        )
        self.decisions[key] = {
            "player": rec.get("player"),
            "kit_key": rec.get("kit_key"),
            "decision": decision,
            "tier": tier or None,
            "label": label,
        }
        await self._swap(interaction, self._next_view())

    # ------------------------------------------------------------------
    # Navigace / zrušení / potvrzení
    # ------------------------------------------------------------------
    async def on_prev(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if self.page > 0:
            self.page -= 1
        await self._swap(interaction, self._next_view())

    async def on_next(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if self.page < len(self.records) - 1:
            self.page += 1
        await self._swap(interaction, self._next_view())

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        self.finished = True
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze odebrat view /checkweb: %s", err)
        await interaction.response.send_message(
            "✖ Zrušeno – nic se nezměnilo.", ephemeral=True
        )

    async def on_apply(self, interaction: discord.Interaction) -> None:
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
                "✅ Rozhodnutí už byla aplikovaná.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # 1) Ověření, že se stav od náhledu nezměnil → aplikujeme PŘESNĚ to,
        #    co admin potvrdil (nikdy nic automaticky navíc).
        fresh = await self.cog._gather(guild)
        if fingerprint_resolvable(fresh["records"]) != self.fingerprint:
            self.finished = True
            await self._finish(interaction, applied=None, stale=True)
            return

        # 2) Aplikace potvrzených rozhodnutí na kanonickou DB.
        players = load_data("players.json", []) or []
        new_players, applied = apply_checkweb_decisions(
            players=players,
            records=fresh["records"],
            decisions=list(self.decisions.values()),
            kit_display=fresh.get("kit_display") or {},
        )
        changed = [
            a for a in applied
            if a.get("ok") and a.get("newTier") != a.get("oldTier")
        ]
        if changed:
            try:
                await self.cog._save_players(new_players)
            except Exception:  # noqa: BLE001 – chyba se zaloguje a řekne
                log.exception("Zápis players.json (/checkweb apply) selhal")

        # 3) Audit – KAŽDÉ rozhodnutí (i keep/ignore) se zapisuje.
        try:
            await log_checkweb_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="apply",
                status="success",
                summary=fresh["summary"],
                website=fresh.get("website_source"),
                errors=fresh.get("errors") or [],
                repairs=applied,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit aplikaci
            log.exception("Auditní zápis (/checkweb apply) selhal")

        self.finished = True
        await self._finish(interaction, applied=applied, stale=False)

    async def _finish(self, interaction, *, applied, stale) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /checkweb – stav se změnil",
                description=(
                    "Mezitím se změnily role, web nebo players.json – "
                    "**nic jsem nezměnil**. Spusť **/checkweb apply** znovu."
                ),
                color=0xEF4444,
            )
        else:
            changed = [
                a for a in (applied or [])
                if a.get("ok") and a.get("newTier") != a.get("oldTier")
            ]
            ok = len(changed)
            total = len(applied or [])
            lines = []
            for a in (applied or [])[:12]:
                if not a.get("ok"):
                    lines.append(f"❌ **{a.get('player')}** · **{a.get('kit')}**: {a.get('error')}")
                elif a.get("newTier") != a.get("oldTier"):
                    lines.append(
                        f"✏️ **{a['player']}** · **{a['kit']}**: "
                        f"{a.get('oldTier') or '—'} → **{a.get('newTier')}** "
                        f"(zdroj: {a.get('source')})"
                    )
                else:
                    lines.append(
                        f"🏛️ **{a.get('player')}** · **{a.get('kit')}**: bez změny ({a.get('reason')})"
                    )
            if total > 12:
                lines.append(f"…a dalších {total - 12} rozhodnutí")
            embed = discord.Embed(
                title="✅ /checkweb – rozhodnutí aplikována",
                description=(
                    f"Změněno v players.json: **{ok} / {total}**\n\n"
                    + "\n".join(lines)
                ),
                color=0x10B981 if ok and total else 0xF59E0B if total else 0xEF4444,
            )
            embed.set_footer(
                text=f"Rozhodnutí: {total} · Audit: data/checkweb_log.json"
            )
        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit potvrzovací zprávu /checkweb: %s", err)
        await interaction.followup.send(embed=embed, ephemeral=True)


class CheckWeb(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    checkweb = app_commands.Group(
        name="checkweb",
        description="Porovná Discord role × players.json × web (jen pro adminy)",
    )

    async def _gather(self, guild: discord.Guild) -> dict:
        """Čerstvá analýza: Discord role × players.json × web (nic nemění)."""
        roles_map = load_data(KIT_ROLES_FILE, {}) or {}
        players = load_data("players.json", []) or []
        kit_display = {str(k).lower(): str(k) for k in get_kits()}
        members = [_member_to_dict(m) for m in await _guild_members(guild)]

        web_players, _sha, error = await github_sync.fetch_players()
        if web_players is None:
            website = None
            website_source = "nedostupný" + (
                f" ({error})" if error else " (bez GITHUB_TOKEN)"
            )
        else:
            website = web_players
            website_source = "GitHub"

        analysis = analyze_checkweb(
            players=players,
            website=website,
            members=members,
            roles_map=roles_map,
            kit_display=kit_display,
        )
        analysis["website_source"] = website_source
        analysis["errors"] = [error] if error else []
        analysis["kit_display"] = kit_display
        return analysis

    async def _save_players(self, players: list) -> None:
        async def _run(tx):
            tx.set("players.json", players)

        await transaction(("players.json",), _run)

    # ------------------------------------------------------------------
    # /checkweb preview
    # ------------------------------------------------------------------
    @checkweb.command(
        name="preview",
        description="Porovná Discord role × players.json × web – nic nemění",
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
            await log_checkweb_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="preview",
                status="success",
                summary=analysis["summary"],
                website=analysis.get("website_source"),
                errors=analysis.get("errors") or [],
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit výpis
            log.exception("Auditní zápis (/checkweb preview) selhal")

        embed = _checkweb_embed(analysis, mode="preview")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /checkweb apply
    # ------------------------------------------------------------------
    @checkweb.command(
        name="apply",
        description="Ukáže záznamy k řešení a po potvrzení opraví players.json",
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
        embed = _checkweb_embed(analysis, mode="apply")

        resolvable = analysis.get("resolvable") or []
        if not resolvable:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = CheckWebApplyView(
            cog=self,
            records=resolvable,
            fingerprint=analysis["fingerprint"],
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CheckWeb(bot))
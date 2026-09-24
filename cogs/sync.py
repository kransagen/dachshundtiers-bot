"""Centrální synchronizace – /sync (check | discord | web | data).

Kanonický tok (jediný zdroj pravdy = ``data/players.json``):

    players.json → Discord role  (/sync discord, RoleSyncService)
                → web/GitHub    (/sync web, services/websync)

- ``/sync check``   – read-only diagnostika: Discord role × players.json × web
                      + lokální integrita (tickety, výsledky, evaly, duplicity).
                      Nález se rozdělí na OK / WARNING / CONFLICT / ERROR a dá
                      se filtrovat podle oblasti (area).
- ``/sync discord`` – preview/apply: DB → Discord role. Nikdy nepřepisuje DB
                      podle Discordu, nepovažuje Discorda za autoritativního,
                      neřeší konflikty automaticky a neignoruje retired tiery.
- ``/sync web``     – preview/apply: DB → web. Selhání GitHubu NIKDY není
                      hlášeno jako úspěch; prázdná DB se na web neposílá.
- ``/sync data``    – hluboká kontrola integrity (dříve /datacheck). Bezpečné
                      opravy (zavření osamoceného ticketu, bezeztrátová
                      normalizace tierů) jen po potvrzení tlačítkem.

Deprecated aliasy (funkční, upozorní na /sync):
  /playersync preview|apply → /sync discord [mode]
  /websync   preview|apply  → /sync web   [mode]
  /checkweb  preview        → /sync check
  /checkweb  apply          → per-záznamová rozhodnutí (jediný Discord→DB
                              writer – explicitní rozhodnutí, doporučuje se
                              /edituser; služba services.checkweb beze změny)
  /datacheck                → /sync data

Architektura: tento cog JE POUZE orchestrace. Business logika zůstává ve
službách (services/checkweb, services/playersync+role_sync, services/websync,
services/datacheck) – NEVYTVÁŘÍME žádné nové služby.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import github_sync
from cogs._shared import (
    admin_gate_error,
    apply_role_actions,
    guild_members,
    kit_display_map,
    member_to_dict,
    save_players,
)
from services.checkweb import (
    STATUS_LABELS,
    STATUSES,
    apply_checkweb_decisions,
    analyze_checkweb,
    fingerprint_resolvable,
    log_checkweb_event,
)
from services.datacheck import (
    DATACHECK_LOG_FILE,
    KIND_LABELS as DC_KIND_LABELS,
    KINDS as DC_KINDS,
    perform_repairs,
    run_datacheck,
)
from services.role_sync import (
    KIND_LABELS as PS_KIND_LABELS,
    KINDS as PS_KINDS,
    analyze_role_sync,
    fingerprint,
    log_playersync_event,
)
from services.websync import (
    KIND_LABELS as WS_KIND_LABELS,
    KINDS as WS_KINDS,
    analyze_websync,
    fingerprint_canonical,
    preview_website,
    sync_website,
)
from storage import DataCorruptionError, load_data
from views import SafeView

log = logging.getLogger("dachshundtiers")

KIT_ROLES_FILE = "kit_roles.json"
SYNC_MESSAGE = "websync: synchronizace hráčů na web"

# Soubory, které čte /datacheck – strict probe před analýzou, aby se korupce
# nepoznala až podle vyprázdněných dat (storage vrací default u poškozeného).
DATACHECK_FILES = (
    "players.json",
    "evals.json",
    "ht_tickets.json",
    "ht_results.json",
    "kit_roles.json",
    "kits.json",
    "testers.json",
)

# --- Prezentační mapování severity (OK/WARNING/CONFLICT/ERROR) ---------------
SEVERITY_ORDER = ("error", "conflict", "warning")
SEVERITY_LABELS = {
    "ok": "✅ OK",
    "warning": "⚠️ WARNING",
    "conflict": "🔶 CONFLICT",
    "error": "❌ ERROR",
}

# Discord role state (services/checkweb statusy)
CHECKWEB_SEVERITY = {
    "DATABASE_MISMATCH": "conflict",
    "MULTIPLE_TIER_ROLES": "conflict",
    "MISSING_DISCORD_ROLE": "warning",
    "WEBSITE_MISMATCH": "warning",
    "UNKNOWN_PLAYER": "warning",
    "UNKNOWN_ROLE": "warning",
    "DUPLICATE_PLAYER": "error",
}
CHECKWEB_AREA = {
    "MISSING_DISCORD_ROLE": "roles",
    "DATABASE_MISMATCH": "roles",
    "MULTIPLE_TIER_ROLES": "roles",
    "UNKNOWN_PLAYER": "roles",
    "UNKNOWN_ROLE": "roles",
    "WEBSITE_MISMATCH": "web",
    "DUPLICATE_PLAYER": "identity",
}

# Web integrita (services/websync)
WEBSYNC_SEVERITY = {
    "missing_player": "warning",
    "wrong_tier": "warning",
    "stale_data": "warning",
    "duplicate_player": "error",
    "invalid_record": "error",
}

# Lokální integrita (services/datacheck má vlastní severity error/warning)
DATACHECK_AREA = {
    "duplicate_players": "identity",
    "duplicate_discord_ids": "identity",
    "duplicate_ign": "identity",
    "duplicate_testers": "identity",
    "duplicate_player_discord_ids": "identity",
    "invalid_tiers": "tiers",
    "retired_tiers_in_modes": "tiers",
    "conflicting_discord_roles": "roles",
    "missing_website_records": "web",
    "invalid_eval_references": "data",
    "orphaned_tickets": "data",
    "orphaned_results": "data",
}

AREAS = ("all", "identity", "tiers", "roles", "web", "data")
AREA_LABELS = {
    "all": "Vše",
    "identity": "Identita",
    "tiers": "Tiery",
    "roles": "Discord role",
    "web": "Web",
    "data": "Data / účty",
}


# ---------------------------------------------------------------------------
# Pomocné (čistá orchestrace – bez vlastní logiky)
# ---------------------------------------------------------------------------
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


def _corrupt_data_files() -> list:
    """Vrátí seznam poškozených souborů z datacheck vesmíru (strict probe)."""
    bad = []
    for name in DATACHECK_FILES:
        try:
            load_data(name, None, strict=True)
        except (DataCorruptionError, UnicodeDecodeError):
            bad.append(name)
    return bad


async def _fetch_website():
    """Přečte web (read-only). Vrátí (players, zdroj, errors)."""
    try:
        players, _sha, error = await github_sync.fetch_players()
    except Exception as err:  # noqa: BLE001 – selhání se převede na hlášku
        log.exception("Čtení webu selhalo")
        return None, f"nedostupný ({err})", [str(err)]
    if players is None:
        source = "nedostupný" + (
            f" ({error})" if error else " (bez GITHUB_TOKEN)"
        )
        return None, source, [error] if error else []
    return players, "GitHub", []


async def _gather_role_sync(guild: discord.Guild) -> dict:
    """Čerstvá analýza: role vs. players.json (nic nemění)."""
    roles_map = load_data(KIT_ROLES_FILE, {}) or {}
    players = load_data("players.json", []) or []
    kit_display = kit_display_map()
    members = [member_to_dict(m) for m in await guild_members(guild)]
    return analyze_role_sync(players, members, roles_map, kit_display)


async def _gather_checkweb(guild: discord.Guild) -> dict:
    """Čerstvá analýza: Discord role × players.json × web (nic nemění)."""
    roles_map = load_data(KIT_ROLES_FILE, {}) or {}
    players = load_data("players.json", []) or []
    kit_display = kit_display_map()
    members = [member_to_dict(m) for m in await guild_members(guild)]

    web_players, website_source, errors = await _fetch_website()
    analysis = analyze_checkweb(
        players=players,
        website=web_players,
        members=members,
        roles_map=roles_map,
        kit_display=kit_display,
    )
    analysis["website_source"] = website_source
    analysis["errors"] = errors
    analysis["kit_display"] = kit_display
    return analysis


# ---------------------------------------------------------------------------
# Embed builders (prezentační vrstva)
# ---------------------------------------------------------------------------
def _append_note(embed: discord.Embed, note: str) -> None:
    if not note:
        return
    current = embed.footer.text if embed.footer else ""
    embed.set_footer(text=(note + "\n" + current).strip())


def _field_value(lines: list, limit: int = 1024) -> str:
    """Hodnota pole embedu z řádků – NIKDY nepřesáhne Discord limit (1024).

    Dlouhé nálezy se neposílají celé: poslední odeslaný řádek se zkrátí a
    doplní se poznámka o zkrácení. Embed tak nemůže spadnout na API 400
    („field value must be 1024 or fewer in length").
    """
    rendered = [str(x) for x in lines]
    joined = "\n".join(rendered)
    if not joined:
        return "_žádné_"
    if len(joined) <= limit:
        return joined
    suffix = f"… (zkráceno; {len(rendered)} záznamů – viz audit log)"
    room = limit - len(suffix) - 1
    if room <= 0:
        return suffix[:limit]
    out = ""
    for line in rendered:
        if room <= 0:
            break
        piece = line[:room] if len(line) <= room else line[: room - 1] + "…"
        out += piece + "\n"
        room -= len(piece) + 1
    return out.rstrip("\n") + "\n" + suffix


def _playersync_embed(analysis: dict, *, mode: str, note: str = "") -> discord.Embed:
    """Embed s přehledem rozdílů (preview / apply)."""
    if not analysis["findings"]:
        embed = discord.Embed(
            title="✅ /sync discord – vše v pořádku",
            description="Role tierů odpovídají players.json. Nemám co opravovat.",
            color=0x10B981,
        )
    else:
        embed = discord.Embed(
            title=f"🔎 /sync discord – {'potvrzení změn' if mode == 'apply' else 'náhled'}",
            description=(
                f"Zkontrolováno párů (člen × kit): **{analysis['checked']}** "
                f"(beze změny: **{analysis.get('unchanged', 0)}**)\n\n"
                + "\n".join(
                    f"{PS_KIND_LABELS[k]}: **{analysis['summary'].get(k, 0)}**"
                    for k in PS_KINDS
                )
            ),
            color=0xF59E0B,
        )
        lines = [f["message"] for f in analysis["findings"]]
        shown = lines[:15]
        if len(lines) > 15:
            shown.append(f"…a dalších {len(lines) - 15} nálezů")
        embed.add_field(name="Zjištěné rozdíly", value=_field_value(shown), inline=False)

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
        embed.set_footer(
            text="Náhled – žádné změny neaplikovány. Pro aplikaci použij /sync discord mode:apply."
        )
    _append_note(embed, note)
    return embed


def _websync_embed(result: dict, *, mode: str, note: str = "") -> discord.Embed:
    """Embed s přehledem rozdílů / výsledkem (preview / apply)."""
    if not result["ok"]:
        embed = discord.Embed(
            title="⚠️ /sync web – nelze pokračovat",
            description=result["message"],
            color=0xEF4444,
        )
        if mode == "apply":
            embed.set_footer(text="Nic se na web neposílalo.")
        _append_note(embed, note)
        return embed

    analysis = result["analysis"] or {}
    if analysis.get("has_issues"):
        embed = discord.Embed(
            title=f"🔎 /sync web – {'potvrzení synchronizace' if mode == 'apply' else 'náhled'}",
            description=(
                f"Kanonická DB: **{analysis['canonical_count']}** hráčů · "
                f"Web: **{analysis['website_count']}** hráčů\n\n"
                + "\n".join(
                    f"{WS_KIND_LABELS[k]}: **{analysis['summary'].get(k, 0)}**"
                    for k in WS_KINDS
                )
            ),
            color=0xF59E0B,
        )
        lines = [f["message"] for f in analysis["findings"]]
        shown = lines[:15]
        if len(lines) > 15:
            shown.append(f"…a dalších {len(lines) - 15} nálezů")
        embed.add_field(name="Rozdíly oproti webu", value=_field_value(shown), inline=False)

        if analysis.get("website_only"):
            extra = [u for u in analysis["website_only"][:15]]
            embed.add_field(
                name="Hráči jen na webu (ne v kanonické DB)",
                value=(
                    "Synchronizace web přepíše kanonickou DB – tito hráči z webu "
                    f"zmizí: {', '.join(extra)}"
                    f"{'…' if len(analysis['website_only']) > 15 else ''}"
                ),
                inline=False,
            )

        if mode == "apply":
            embed.set_footer(
                text=(
                    f"Synchronizace NAHRADÍ players.json na webu kanonickou DB "
                    f"({analysis['canonical_count']} záznamů) – potvrď tlačítkem níže."
                )
            )
        else:
            embed.set_footer(
                text="Náhled – nic se neposílalo. Pro potvrzení použij /sync web mode:apply."
            )
    else:
        embed = discord.Embed(
            title="✅ /sync web – web je v synchronizaci",
            description=(
                f"Kanonická DB (**{analysis['canonical_count']}** hráčů) odpovídá "
                "webu – nemám co opravovat."
            ),
            color=0x10B981,
        )
    _append_note(embed, note)
    return embed


def _datacheck_embed(report: dict, *, note: str = "") -> discord.Embed:
    if not report["has_issues"]:
        embed = discord.Embed(
            title="✅ /sync data – vše v pořádku",
            description="Všechny databáze jsou konzistentní – nemám co hlásit.",
            color=0x10B981,
        )
        _append_note(embed, note)
        return embed

    summary_text = "\n".join(
        f"{DC_KIND_LABELS[k]}: **{report['summary'].get(k, 0)}**" for k in DC_KINDS
    ) or "_žádné nálezy_"
    embed = discord.Embed(
        title="🔍 /sync data – kontrola integrity",
        description=summary_text,
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
    _append_note(embed, note)
    return embed


def _repair_result_embed(result: dict) -> discord.Embed:
    ok = bool(result.get("ok"))
    embed = discord.Embed(
        title="🔧 /sync data – bezpečné opravy",
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
            value=_field_value(str(e) for e in result["errors"][:15]),
            inline=False,
        )
    embed.set_footer(text=f"Zapsáno do data/{DATACHECK_LOG_FILE} (audit).")
    return embed


def _checkweb_compact(record: dict) -> str:
    """Jednořádkový přehled záznamu pro náhled."""
    db = record.get("db") or "—"
    dc = ", ".join(record.get("discord") or []) or "—"
    web = record.get("web") or "—"
    marker = "•"
    if record.get("status") == "MATCH":
        marker = "✅"
    elif record.get("status") == "MISSING_DISCORD_ROLE":
        marker = "➕"
    elif record.get("status") == "DATABASE_MISMATCH":
        marker = "✏️"
    elif record.get("status") == "WEBSITE_MISMATCH":
        marker = "🌐"
    elif record.get("status") == "MULTIPLE_TIER_ROLES":
        marker = "🔁"
    elif record.get("status") == "UNKNOWN_ROLE":
        marker = "❓"
    elif record.get("status") == "UNKNOWN_PLAYER":
        marker = "👤"
    elif record.get("status") == "DUPLICATE_PLAYER":
        marker = "👥"
    return (
        f"{marker} **{record['player']}** · **{record['kit']}**: "
        f"DB {db} · DC {dc} · WEB {web}"
    )


def _checkweb_embed(analysis: dict, *, mode: str, note: str = "") -> discord.Embed:
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
        summary_text = "\n".join(
            f"{STATUS_LABELS[s]}: **{summary.get(s, 0)}**" for s in STATUSES
        )
        embed = discord.Embed(
            title=f"🔎 /checkweb – {'potvrzení' if mode == 'apply' else 'náhled'}",
            description=(
                f"Zkontrolováno záznamů (hráč × kit): **{analysis['checked']}**\n"
                f"Web: {source}\n\n"
                + summary_text
            ),
            color=0xF59E0B,
        )
        lines = [_checkweb_compact(r) for r in records if r["status"] != "MATCH"]
        shown = lines[:12]
        if len(lines) > 12:
            shown.append(f"…a dalších {len(lines) - 12} nálezů")
        embed.add_field(name="Nálezy", value=_field_value(shown), inline=False)
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
                "a potvrď tlačítkem. Web se nemění (na to je /sync web)."
            )
        else:
            embed.set_footer(
                text="Žádný záznam nevyžaduje rozhodnutí – opravy jsou ruční "
                "(viz nálezy) nebo přes /sync web apply."
            )
    else:
        embed.set_footer(
            text="Náhled – žádné změny neaplikovány. JEDINÝ zapisovatel je "
            "/checkweb apply s potvrzením."
        )
    _append_note(embed, note)
    return embed


def _check_embed(
    items: list,
    *,
    counts: dict,
    website_source: str,
    area: str,
    corrupt: list,
    repairable_count: int,
    note: str = "",
) -> discord.Embed:
    """Embed /sync check – nálezy seskupené podle severity."""
    if corrupt:
        embed = discord.Embed(
            title="❌ /sync check – poškozená data",
            description=(
                "Následující soubory nejsou platný JSON – kontrola se "
                f"NESPUSTILA, nic se nemění:\n\n{', '.join(f'`{f}`' for f in corrupt)}\n\n"
                "Poškozené soubory se nikdy automaticky nepřepisují – oprav je "
                "ručně a spusť /sync check znovu."
            ),
            color=0xEF4444,
        )
        embed.set_footer(text="DataCorruptionError – bezpečný abort.")
        return embed

    total = sum(counts.values())
    if total == 0:
        embed = discord.Embed(
            title="✅ /sync check – vše v pořádku",
            description=(
                f"Nalezeno **0** problémů.\nZdroj dat webu: **{website_source}**."
            ),
            color=0x10B981,
        )
        embed.set_footer(
            text="Read-only – nic se nemění. Audit: data/checkweb_log.json "
            "a data/datacheck_log.json."
        )
        _append_note(embed, note)
        return embed

    severity_lines = "\n".join(
        f"{SEVERITY_LABELS[s]}: **{counts.get(s, 0)}**" for s in SEVERITY_ORDER
    )
    title_suffix = f" ({AREA_LABELS.get(area, area)})" if area != "all" else ""
    embed = discord.Embed(
        title=f"🔎 /sync check – náhled stavu{title_suffix}",
        description=(
            f"Zdroj dat webu: **{website_source}**\nNálezů celkem: **{total}**\n\n"
            + severity_lines
        ),
        color=0xEF4444 if counts.get("error") else 0xF59E0B,
    )
    for sev in SEVERITY_ORDER:
        group = [i for i in items if i["severity"] == sev]
        if not group:
            continue
        lines = [i["message"] for i in group[:10]]
        if len(group) > 10:
            lines.append(f"…a dalších {len(group) - 10} nálezů")
        embed.add_field(
            name=f"{SEVERITY_LABELS[sev]} ({len(group)})",
            value=_field_value(lines),
            inline=False,
        )
    if repairable_count:
        embed.add_field(
            name="🔧 Bezpečné opravy",
            value=(
                f"{repairable_count} oprav je dostupných přes **/sync data** "
                "(potvrzení tlačítkem)."
            ),
            inline=False,
        )
    embed.set_footer(
        text="Read-only – nic se nemění. Audit: data/checkweb_log.json "
        "a data/datacheck_log.json."
    )
    _append_note(embed, note)
    return embed


# ---------------------------------------------------------------------------
# Potvrzovací view (přesunuto beze změny logiky ze starých cogů)
# ---------------------------------------------------------------------------
class SyncDiscordConfirmView(SafeView):
    """Tlačítko „Potvrdit a aplikovat" pro /sync discord apply."""

    def __init__(self, *, analysis: dict):
        super().__init__(timeout=120)
        self.fingerprint = analysis["fingerprint"]
        self.finished = False

    @discord.ui.button(
        label="✅ Potvrdit a aplikovat",
        style=discord.ButtonStyle.success,
        custom_id="sync_discord_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.finished:
            return await interaction.response.send_message(
                "✅ Změny už byly aplikované.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # 1) Ověření, že se stav od náhledu nezměnil → aplikujeme PŘESNĚ to,
        #    co admin potvrdil (nikdy nic automaticky navíc).
        fresh = await _gather_role_sync(interaction.guild)
        if fingerprint(fresh["actions"]) != self.fingerprint:
            self.finished = True
            applied = []
            await self._finish(interaction, applied=applied, stale=True)
            return

        # 2) Aplikace akcí – každá zvlášť, chyby se nikdy nešíří dál.
        applied = await apply_role_actions(interaction.guild, fresh["actions"])

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
                title="🔄 /sync discord – stav se změnil",
                description=(
                    "Mezitím se změnily role nebo players.json – **nic jsem "
                    "neaplikoval**. Spusť **/sync discord mode:apply** znovu."
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
                title="✅ /sync discord – změny aplikovány",
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


class SyncWebConfirmView(SafeView):
    """Tlačítko „Potvrdit a nahrát na web" pro /sync web apply."""

    def __init__(self, *, canonical_fingerprint: str):
        super().__init__(timeout=120)
        self.fingerprint = canonical_fingerprint
        self.finished = False

    @discord.ui.button(
        label="✅ Potvrdit a nahrát na web",
        style=discord.ButtonStyle.success,
        custom_id="sync_web_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
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
            await self._finish(interaction, result=None, stale=True)
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
                title="🔄 /sync web – stav se změnil",
                description=(
                    "Kanonická players.json se mezitím změnila – **nic jsem "
                    "na web neposlal**. Spusť **/sync web mode:apply** znovu."
                ),
                color=0xEF4444,
            )
        else:
            ok = bool(result and result["ok"])
            embed = discord.Embed(
                title=(
                    "✅ /sync web – web synchronizován"
                    if ok
                    else "❌ /sync web – synchronizace selhala"
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


class SyncDataRepairView(SafeView):
    """Tlačítko „Aplikovat bezpečné opravy" pro /sync data."""

    def __init__(self):
        super().__init__(timeout=120)
        self.finished = False

    @discord.ui.button(
        label="🔧 Aplikovat bezpečné opravy",
        style=discord.ButtonStyle.success,
        custom_id="sync_data_repair",
    )
    async def repair(self, interaction: discord.Interaction, button) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.finished:
            return await interaction.response.send_message(
                "✅ Opravy už proběhly.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        self.finished = True

        # Čerstvá kontrola (stav se mohl změnit od náhledu).
        try:
            report = await run_datacheck(
                channel_exists=_channel_exists_resolver(interaction.guild),
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
            )
        except DataCorruptionError as err:
            embed = discord.Embed(
                title="❌ /sync data – poškozená data",
                description=(
                    "Opravy se NEPROVEDLY – soubor není platný JSON "
                    f"(`{err}`). Poškozené soubory se nikdy automaticky "
                    "nepřepisují."
                ),
                color=0xEF4444,
            )
            try:
                if interaction.message is not None:
                    await interaction.message.edit(embed=embed, view=None)
            except (discord.HTTPException, discord.Forbidden) as err2:
                log.warning("Nelze upravit zprávu /sync data: %s", err2)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        r = report["repairable"]
        if not r["close_ticket"] and not r["normalize_tier"]:
            embed = discord.Embed(
                title="✅ /sync data – už není co opravit",
                description="Kontrola po náhledu nehlásí žádné bezpečné opravy.",
                color=0x10B981,
            )
        else:
            try:
                result = await perform_repairs(
                    close_ticket_ids=r["close_ticket"],
                    tier_fixes=r["normalize_tier"],
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                )
            except DataCorruptionError as err:
                embed = discord.Embed(
                    title="❌ /sync data – poškozená data",
                    description=(
                        "Opravy se NEPROVEDLY – soubor není platný JSON "
                        f"(`{err}`). Poškozené soubory se nikdy automaticky "
                        "nepřepisují."
                    ),
                    color=0xEF4444,
                )
            else:
                embed = _repair_result_embed(result)

        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit zprávu /sync data: %s", err)
        await interaction.followup.send(embed=embed, ephemeral=True)


class CheckWebApplyView(SafeView):
    """Per-záznamová rozhodnutí pro /checkweb apply.

    U KONFLIKTU (víc tier rolí jednoho kitu) se nikdy nevybírá automaticky –
    každá Discord role se nabídne zvlášť ([Use Discord]). Při potvrzení se
    stav znovu analyzuje a porovná otisk s tím, co admin viděl; liší-li se,
    nic se neaplikuje.
    """

    def __init__(self, *, guild, records, fingerprint, decisions=None, page=0):
        super().__init__(timeout=300)
        self.guild = guild
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
        embed.add_field(
            name="Status", value=rec.get("label", rec["status"]), inline=False
        )
        decision = self.decisions.get(self._key(rec))
        if decision:
            embed.add_field(
                name="Rozhodnuto", value=decision.get("label", ""), inline=False
            )
        embed.set_footer(
            text="„Use Discord“ změní JEN tier v players.json (bez historie). "
            "Web se nemění – na to je /sync web."
        )
        return embed

    def _next_view(self) -> "CheckWebApplyView":
        return CheckWebApplyView(
            guild=self.guild,
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
    # Rozhodnutí (select) – callbacky bez dekorátorů, přesně jako originál
    # (komponenty se staví tam, kde je /checkweb apply používá).
    # ------------------------------------------------------------------
    async def on_decision(self, interaction: discord.Interaction) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
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

    async def on_prev(self, interaction: discord.Interaction) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.page > 0:
            self.page -= 1
        await self._swap(interaction, self._next_view())

    async def on_next(self, interaction: discord.Interaction) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.page < len(self.records) - 1:
            self.page += 1
        await self._swap(interaction, self._next_view())

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
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
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.finished:
            return await interaction.response.send_message(
                "✅ Rozhodnutí už byla aplikovaná.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # 1) Ověření, že se stav od náhledu nezměnil → aplikujeme PŘESNĚ to,
        #    co admin potvrdil (nikdy nic automaticky navíc).
        fresh = await _gather_checkweb(self.guild)
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
            a
            for a in applied
            if a.get("ok") and a.get("newTier") != a.get("oldTier")
        ]
        if changed:
            try:
                await save_players(new_players)
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
                a
                for a in (applied or [])
                if a.get("ok") and a.get("newTier") != a.get("oldTier")
            ]
            ok = len(changed)
            total = len(applied or [])
            lines = []
            for a in (applied or [])[:12]:
                if not a.get("ok"):
                    lines.append(
                        f"❌ **{a.get('player')}** · **{a.get('kit')}**: {a.get('error')}"
                    )
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


# ---------------------------------------------------------------------------
# Cog – orchestrace (business logika zůstává ve službách)
# ---------------------------------------------------------------------------
class Sync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    sync = app_commands.Group(
        name="sync",
        description="Centrální synchronizace: check / discord / web / data",
    )

    # ------------------------------------------------------------------
    # /sync check
    # ------------------------------------------------------------------
    @sync.command(
        name="check",
        description="Read-only diagnostika: Discord × players.json × web + integrita",
    )
    @app_commands.describe(area="Kterou oblast zobrazit (výchozí: vše)")
    @app_commands.choices(
        area=[
            app_commands.Choice(name=AREA_LABELS[a], value=a) for a in AREAS
        ]
    )
    async def sync_check(self, interaction: discord.Interaction, area: str = "all") -> None:
        await self._run_check(interaction, area=area)

    async def _run_check(
        self, interaction: discord.Interaction, area: str = "all", *, note: str = ""
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        corrupt = _corrupt_data_files()
        if corrupt:
            embed = _check_embed(
                [], counts={"ok": 0, "warning": 0, "conflict": 0, "error": 0},
                website_source="n/a", area=area, corrupt=corrupt,
                repairable_count=0, note=note,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        players = load_data("players.json", []) or []
        roles_map = load_data(KIT_ROLES_FILE, {}) or {}
        kit_display = kit_display_map()
        members = [member_to_dict(m) for m in await guild_members(interaction.guild)]

        web_players, website_source, errors = await _fetch_website()

        # 1) 3-cestná analýza (Discord × DB × web) – audit pokračuje ve
        #    stávajícím checkweb_log.json (kontinuita).
        cw = analyze_checkweb(
            players=players,
            website=web_players,
            members=members,
            roles_map=roles_map,
            kit_display=kit_display,
        )
        try:
            await log_checkweb_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="preview",
                status="success",
                summary=cw["summary"],
                website=website_source,
                errors=errors,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit výpis
            log.exception("Auditní zápis (/sync check – checkweb) selhal")

        # 2) Web integrita – jen když se web podařilo přečíst (jinak by
        #    analyze_websync hlásil falešné missing_player).
        ws = (
            analyze_websync(players, web_players)
            if web_players is not None
            else None
        )

        # 3) Lokální integrita – audit pokračuje v datacheck_log.json.
        dc = await run_datacheck(
            channel_exists=_channel_exists_resolver(interaction.guild),
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )

        items = []
        for rec in cw["records"]:
            status = rec.get("status")
            if status == "MATCH":
                continue
            items.append(
                {
                    "severity": CHECKWEB_SEVERITY.get(status, "warning"),
                    "area": CHECKWEB_AREA.get(status, "roles"),
                    "message": rec["message"],
                }
            )
        if ws:
            for f in ws["findings"]:
                items.append(
                    {
                        "severity": WEBSYNC_SEVERITY.get(f["kind"], "warning"),
                        "area": "web",
                        "message": f["message"],
                    }
                )
        for f in dc["findings"]:
            items.append(
                {
                    "severity": (
                        "error" if f.get("severity") == "error" else "warning"
                    ),
                    "area": DATACHECK_AREA.get(f["kind"], "data"),
                    "message": f["message"],
                }
            )

        if area != "all":
            items = [i for i in items if i["area"] == area]

        counts = {"ok": 0, "warning": 0, "conflict": 0, "error": 0}
        for i in items:
            counts[i["severity"]] += 1

        embed = _check_embed(
            items,
            counts=counts,
            website_source=website_source,
            area=area,
            corrupt=[],
            repairable_count=dc["repairable_count"],
            note=note,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /sync discord
    # ------------------------------------------------------------------
    @sync.command(
        name="discord",
        description="Synchronizace DB → Discord role (preview / po potvrzení apply)",
    )
    @app_commands.describe(mode="preview = jen analýza · apply = po potvrzení aplikuje")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="preview", value="preview"),
            app_commands.Choice(name="apply", value="apply"),
        ]
    )
    async def sync_discord(self, interaction: discord.Interaction, mode: str) -> None:
        await self._run_discord(interaction, mode=mode)

    async def _run_discord(
        self, interaction: discord.Interaction, mode: str, *, note: str = ""
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        analysis = await _gather_role_sync(interaction.guild)
        if mode == "preview":
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
            embed = _playersync_embed(analysis, mode="preview", note=note)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = _playersync_embed(analysis, mode="apply", note=note)
        if not analysis["findings"]:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not analysis["has_actions"]:
            embed.set_footer(
                text="Žádné změny nelze aplikovat automaticky – viz nálezy "
                "(oprava je ruční)."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = SyncDiscordConfirmView(analysis=analysis)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ------------------------------------------------------------------
    # /sync web
    # ------------------------------------------------------------------
    @sync.command(
        name="web",
        description="Synchronizace DB → web/GitHub (preview / po potvrzení apply)",
    )
    @app_commands.describe(mode="preview = jen analýza · apply = po potvrzení nahraje")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="preview", value="preview"),
            app_commands.Choice(name="apply", value="apply"),
        ]
    )
    async def sync_web(self, interaction: discord.Interaction, mode: str) -> None:
        await self._run_web(interaction, mode=mode)

    async def _run_web(
        self, interaction: discord.Interaction, mode: str, *, note: str = ""
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        canonical = load_data("players.json", []) or []
        result = await preview_website(
            canonical=canonical,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        embed = _websync_embed(result, mode=mode, note=note)

        if not result["ok"]:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not result["analysis"]["has_issues"] or mode != "apply":
            # preview = read-only náhled (věrné chování /websync): view jen
            # při mode:"apply" s rozdíly
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = SyncWebConfirmView(
            canonical_fingerprint=result["fingerprint"]
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ------------------------------------------------------------------
    # /sync data
    # ------------------------------------------------------------------
    @sync.command(
        name="data",
        description="Hluboká kontrola integrity dat (bezpečné opravy po potvrzení)",
    )
    async def sync_data(self, interaction: discord.Interaction) -> None:
        await self._run_data(interaction)

    async def _run_data(self, interaction: discord.Interaction, *, note: str = "") -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        corrupt = _corrupt_data_files()
        if corrupt:
            embed = discord.Embed(
                title="❌ /sync data – poškozená data",
                description=(
                    "Následující soubory nejsou platný JSON – kontrola se "
                    f"NESPUSTILA, nic se nemění:\n\n{', '.join(f'`{f}`' for f in corrupt)}\n\n"
                    "Poškozené soubory se nikdy automaticky nepřepisují – "
                    "oprav je ručně a spusť /sync data znovu."
                ),
                color=0xEF4444,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        try:
            report = await run_datacheck(
                channel_exists=_channel_exists_resolver(interaction.guild),
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
            )
        except DataCorruptionError as err:
            embed = discord.Embed(
                title="❌ /sync data – poškozená data",
                description=(
                    "Kontrola se NEPROVEDLA – soubor není platný JSON "
                    f"(`{err}`). Poškozené soubory se nikdy automaticky "
                    "nepřepisují."
                ),
                color=0xEF4444,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = _datacheck_embed(report, note=note)
        if report["repairable_count"]:
            view = SyncDataRepairView()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /checkweb apply – per-záznamová rozhodnutí (přesunuto beze změny)
    # ------------------------------------------------------------------
    async def _run_checkweb_apply(
        self, interaction: discord.Interaction, *, note: str = ""
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        analysis = await _gather_checkweb(interaction.guild)
        embed = _checkweb_embed(analysis, mode="apply", note=note)

        resolvable = analysis.get("resolvable") or []
        if not resolvable:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        view = CheckWebApplyView(
            guild=interaction.guild,
            records=resolvable,
            fingerprint=analysis["fingerprint"],
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ------------------------------------------------------------------
    # Deprecated aliasy (funkční, chovají se identicky jako /sync)
    # ------------------------------------------------------------------
    playersync = app_commands.Group(
        name="playersync",
        description="DEPRECATED – použij /sync discord",
    )

    @playersync.command(
        name="preview",
        description="DEPRECATED – použij /sync discord mode:preview",
    )
    async def playersync_preview(self, interaction: discord.Interaction) -> None:
        await self._run_discord(
            interaction,
            mode="preview",
            note="⚠️ Deprecated – použij /sync discord mode:preview.",
        )

    @playersync.command(
        name="apply",
        description="DEPRECATED – použij /sync discord mode:apply",
    )
    async def playersync_apply(self, interaction: discord.Interaction) -> None:
        await self._run_discord(
            interaction,
            mode="apply",
            note="⚠️ Deprecated – použij /sync discord mode:apply.",
        )

    websync = app_commands.Group(
        name="websync",
        description="DEPRECATED – použij /sync web",
    )

    @websync.command(
        name="preview",
        description="DEPRECATED – použij /sync web mode:preview",
    )
    async def websync_preview(self, interaction: discord.Interaction) -> None:
        await self._run_web(
            interaction,
            mode="preview",
            note="⚠️ Deprecated – použij /sync web mode:preview.",
        )

    @websync.command(
        name="apply",
        description="DEPRECATED – použij /sync web mode:apply",
    )
    async def websync_apply(self, interaction: discord.Interaction) -> None:
        await self._run_web(
            interaction,
            mode="apply",
            note="⚠️ Deprecated – použij /sync web mode:apply.",
        )

    checkweb = app_commands.Group(
        name="checkweb",
        description="DEPRECATED – použij /sync check",
    )

    @checkweb.command(
        name="preview",
        description="DEPRECATED – použij /sync check",
    )
    async def checkweb_preview(self, interaction: discord.Interaction) -> None:
        await self._run_check(
            interaction,
            area="all",
            note="⚠️ Deprecated – použij /sync check.",
        )

    @checkweb.command(
        name="apply",
        description="DEPRECATED – per-záznamová rozhodnutí (doporučuje se /edituser)",
    )
    async def checkweb_apply(self, interaction: discord.Interaction) -> None:
        await self._run_checkweb_apply(
            interaction,
            note=(
                "⚠️ Deprecated – jediný Discord→DB writer. Kanonický směr je "
                "DB→Discord→web; doporučuje se /edituser."
            ),
        )

    @app_commands.command(
        name="datacheck",
        description="DEPRECATED – použij /sync data",
    )
    async def datacheck(self, interaction: discord.Interaction) -> None:
        await self._run_data(
            interaction,
            note="⚠️ Deprecated – použij /sync data.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sync(bot))
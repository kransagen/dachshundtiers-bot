"""Centrální synchronizace – /sync (check | discord | web | data).

Fáze C – Discord je JEDINÁ autorita aktuálních tier rolí:

    Discord tier role → PostgreSQL mirror  (/sync discord, DiscordSyncService)
    players.json      → web/GitHub         (/sync web, services/websync)
    /result /topresult /linkdiscord        (explicitní operace s rolemi)

- ``/sync check``     – read-only diagnostika: Discord × players.json × web
                        + lokální integrita (tickety, výsledky, evaly, duplicity).
                        Nález se rozdělí na OK / WARNING / CONFLICT / ERROR a dá
                        se filtrovat podle oblasti (area).
- ``/sync discord``   – OBSERVE-ONLY: Discord → PostgreSQL mirror. Nikdy nemění
                        Discord role, neopravuje anomálie automaticky a nikdy
                        nepoužívá DB k rozsuzování Discordu.
- ``/sync discord-rollback`` – jediná mutace rolí normálního syncu: invertuje
                        PŘESNĚ akce historického aplikovaného syncu z auditu
                        (obnova po chybném odsouhlasení; jinak se role nemění).
- ``/sync web``       – preview/apply: DB → web. Selhání GitHubu NIKDY není
                        hlášeno jako úspěch; prázdná DB se na web neposílá.
- ``/sync data``      – hluboká kontrola integrity (dříve /datacheck). Bezpečné
                        opravy (zavření osamoceného ticketu, bezeztrátová
                        normalizace tierů) jen po potvrzení tlačítkem.

Deprecated aliasy (funkční, upozorní na /sync):
  /playersync preview|apply → /sync discord (observe-only)
  /websync   preview|apply  → /sync web   [mode]
  /checkweb  preview        → /sync check
  /checkweb  apply          → per-záznamová rozhodnutí, už jen NÁHLED (F-FIX:
                              zápis JSON odstraněn; změnu provede /sync discord)
  /datacheck                → /sync data

Architektura: tento cog JE POUZE orchestrace. Business logika zůstává ve
službách (services/checkweb, services/playersync+role_sync, services/websync,
services/datacheck, db/services/mirror_sync) – NEVYTVÁŘÍME žádné nové služby.
"""

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import github_sync
from cogs._shared import (
    admin_gate_error,
    apply_rollback_actions,
    guild_members,
    kit_display_map,
    member_to_dict,
)
from db.repositories.sync_audit import (
    SYNC_ACTION_APPLIED,
    SYNC_ACTION_FAILED,
    SYNC_RUN_FAILED,
    SYNC_RUN_PARTIAL,
    SYNC_RUN_SUCCESS,
    SyncActionRepository,
)
from db.services import DiscordSyncOutcome, DiscordSyncService
from db.services.session import transaction
from services.checkweb import (
    STATUS_LABELS,
    STATUSES,
    apply_checkweb_decisions,
    analyze_checkweb,
    build_discord_import_decisions,
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
from services.kit_roles import get_all_kit_role_maps
from services.player_export import export_players
from services.role_sync import (
    build_rollback_plan,
    find_rollback_target,
    fingerprint,
    get_playersync_log,
    log_playersync_rollback_event,
    verify_rollback_plan,
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


async def _canonical_for_export(session_factory) -> list:
    """Canonical players pro web/GitHub export: PostgreSQL → players.json fallback.

    Export je export-only kanál: při výpadku DB nesmí selhat, ale fallback je
    hlasitý (log.exception) a nikdy se nepropírá do autoritativních cest.
    """
    if session_factory is not None:
        try:
            return await export_players(session_factory)
        except Exception:  # noqa: BLE001 – výpadek DB nesmí zablokovat export
            log.exception(
                "Canonical export z PostgreSQL selhal – pokračuji z data/players.json"
            )
    return load_data("players.json", []) or []


async def _canonical_players(session_factory) -> list:
    """'DB' strana checkweb analýzy = canonical data z PostgreSQL (F-FIX).

    V DB režimu je ``players.json`` export-only a nesmí být čten jako zdroj
    current tieru, takže výpadek DB vyvolá výjimku — žádný tichý fallback.
    Bez ``session_factory`` běží legacy JSON režim, kde je ``players.json``
    jediný dostupný store.
    """
    if session_factory is not None:
        return await export_players(session_factory)
    return load_data("players.json", []) or []


async def _gather_checkweb(
    guild: discord.Guild, *, session_factory=None
) -> dict:
    """Čerstvá analýza: Discord role × PostgreSQL mirror × web (nic nemění)."""
    roles_map = await get_all_kit_role_maps(session_factory=session_factory)
    players = await _canonical_players(session_factory)
    kit_display = await kit_display_map(session_factory=session_factory)
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


# ---------------------------------------------------------------------------
# Bezpečné formátování diagnostických výpisů (embed pack)
#
# Discord limity: název pole <= 256, hodnota pole <= 1024, <= 25 polí na
# embed, celkem <= 6000 znaků na embed. Dlouhé seznamy se NIKDY nezkracují –
# rozdělují se na víc polí / embedů (na hranicích řádků), takže žádná
# diagnostika se neztrácí a odpověď nemůže spadnout na API 400.
# ---------------------------------------------------------------------------
EMBED_TITLE_LIMIT = 256
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FIELD_COUNT_LIMIT = 25
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FOOTER_LIMIT = 2048
EMBED_TOTAL_LIMIT = 6000
_EMBED_TOTAL_BUDGET = EMBED_TOTAL_LIMIT - 200  # rezerva na overhead embedu
_CONTINUATION_PREFIX = "» "


def _clip(text: str, limit: int) -> str:
    """Defenzivní ořez syntetického textu (title/description/footer/název).

    Používá se JEN na texty, které samy o sobě nejsou diagnostikou (county,
    popisy, názvy sekcí) – ty jsou vždy krátké a ořez je pojistka.
    Diagnostické řádky se NIKDY neořezávají (řeší to _pack_section).
    """
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _split_long_line(line: str, limit: int, prefix: str) -> list:
    """Rozdělí jeden příliš dlouhý řádek na kusy <= ``limit``.

    Dělí se na hranicích slov (jinak tvrdý řez). Pokračování dostává prefix
    ``prefix`` (čtenář pozná, že řádek pokračuje) – VŠECHNY znaky původního
    řádku zůstávají zachované (žádné zkrácení ani ztráta mezer).
    """
    if len(line) <= limit:
        return [line]
    budget = limit - len(prefix)
    out = []
    rest = line
    while len(rest) > budget:
        cut = rest.rfind(" ", len(prefix), budget)
        if cut < len(prefix):
            cut = budget
        out.append(rest[:cut])
        rest = rest[cut:]
        if rest:
            rest = prefix + rest
    if rest:
        out.append(rest)
    return out


def _pack_section(name: str, lines) -> list:
    """Rozdělí řádky sekce na pole ``(name, value)`` do limitu 1024 znaků.

    Dělí se na hranicích řádků; řádek delší než limit se rozdělí na
    pokračování (prefix „» ") bez ztráty znaků. Vrací alespoň jedno pole
    (prázdná sekce → „_žádné_"), nikdy nepřesahující Discord limity.
    """
    limit = EMBED_FIELD_VALUE_LIMIT
    name = _clip(str(name), EMBED_FIELD_NAME_LIMIT)
    fields = []
    current = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            fields.append((name, "\n".join(current)))
            current = []
            current_len = 0

    for raw in lines:
        for chunk in _split_long_line(str(raw), limit, _CONTINUATION_PREFIX):
            add = len(chunk) + (1 if current else 0)
            if current and current_len + add > limit:
                flush()
            if not current:
                current.append(chunk)
                current_len = len(chunk)
            else:
                current.append(chunk)
                current_len += add

    flush()
    if not fields:
        fields.append((name, "_žádné_"))
    return fields


def build_embed_pack(
    *,
    title: str,
    description: str = "",
    color: int = 0xF59E0B,
    footer: str = "",
    sections=(),
    continuation_title: str = "…pokračování",
    continuation_footer_suffix: str = " · …pokračování",
) -> list:
    """Postaví embed pack z diagnostických sekcí; VŠECHNY informace zůstanou.

    ``sections``: [(název, [řádky, …]), …] – každá sekce se rozdělí na pole
    (<=1024 znaků) a příp. víc embedů (<=25 polí, celkem <=6000 znaků).
    Pokračování se pozná podle titulku „…pokračování". Vrací >= 1 embed.
    title/description/footer jsou syntetické texty (jen defenzivně oříznuté
    na Discord limity, nikdy neobsahují samotné nálezy).
    """
    title = _clip(str(title), EMBED_TITLE_LIMIT)
    description = _clip(str(description), EMBED_DESCRIPTION_LIMIT)
    footer = _clip(str(footer), EMBED_FOOTER_LIMIT)

    records = []
    for name, lines in sections:
        records.extend(_pack_section(name, lines))

    embeds = []
    page = 0
    idx = 0
    while True:
        if page == 0:
            embed = discord.Embed(title=title, description=description, color=color)
            if footer:
                embed.set_footer(text=footer)
        else:
            embed = discord.Embed(
                title=continuation_title,
                description=f"…pokračování přehledu (část {page + 1}).",
                color=color,
            )
            if footer:
                embed.set_footer(text=footer + continuation_footer_suffix)
        used = (
            len(embed.title or "")
            + len(embed.description or "")
            + len(embed.footer.text if embed.footer else "")
            + 40  # overhead embedu (barva, timestamp, …)
        )
        while idx < len(records) and len(embed.fields) < EMBED_FIELD_COUNT_LIMIT:
            name, value = records[idx]
            cost = len(name) + len(value) + 2  # název + hodnota + oddělovač
            if used + cost > _EMBED_TOTAL_BUDGET and len(embed.fields) > 0:
                break
            embed.add_field(name=name, value=value, inline=False)
            used += cost
            idx += 1
        embeds.append(embed)
        if idx >= len(records):
            break
        page += 1
    return embeds


async def _send_embed_pack(target, embeds, *, view=None, ephemeral=True) -> None:
    """Odešle embed pack (max 10 embedů na zprávu; view jen u první zprávy).

    Pokud pack nesedí do jedné zprávy, pokračování jde další zprávou –
    diagnostika se nikdy neztrácí a odpověď zůstává ephemeral. HTTPException
    se tu NELOVÍ – chyba odeslání musí propadnout nahoru.

    Volitelné parametry se předávají jen když nejsou None – novější discord.py
    odmítá explicitní ``view=None`` (TypeError).
    """
    for i in range(0, len(embeds), 10):
        chunk = embeds[i : i + 10]
        kwargs = {"embeds": chunk}
        if ephemeral is not None:
            kwargs["ephemeral"] = ephemeral
        if i == 0 and view is not None:
            kwargs["view"] = view
        await target.send(**kwargs)


async def _edit_embed_pack(interaction, embeds) -> None:
    """Nahradí embedy stávající zprávy packem (edit = max 10 embedů).

    Selhání editu se jen zaloguje – výsledek stejně dorazí followup.em
    (odpověď uživatele se tím nikdy neztratí).
    """
    try:
        if interaction.message is not None:
            await interaction.message.edit(embeds=embeds[:10], view=None)
    except (discord.HTTPException, discord.Forbidden) as err:
        log.warning("Nelze upravit potvrzovací zprávu: %s", err)


def _fmt_ts(ts_ms) -> str:
    """Čitelný formát času z auditního ts (ms epoch)."""
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).strftime(
            "%d.%m.%Y %H:%M:%S"
        )
    except (TypeError, ValueError, OSError):
        return str(ts_ms)


def _rollback_embed(
    plan: dict, *, mode: str, note: str = "", verification: dict | None = None
) -> list:
    """Shrnutí rollbacku /sync discord (dry run / potvrzení) – nic nemění.

    Souhrn se seskupuje podle PŮVODNÍ operace auditu (Original REMOVE →
    Rollback ADD, Original ADD → Rollback REMOVE) a ukazuje výsledek finální
    bezpečnostní kontroly (memberId + roleId + op + důkaz ok=True).
    """
    total = len(plan["actions"])
    remove_to_add = sum(
        1 for a in plan["actions"] if a.get("original_op") == "remove"
    )
    add_to_remove = total - remove_to_add
    verified = (verification or {}).get("verified", total)
    sum_ok = bool((verification or {}).get("sum_matches", True))
    verified_ok = verified == total and sum_ok
    if mode == "apply":
        footer = (
            "Rollback se spustí JEN po potvrzení tlačítkem níže – inverze "
            "přesně těch akcí, které cílový sync úspěšně aplikoval."
        )
    else:
        footer = "Dry run – žádné Discord změny neprovedeny."
    embed = discord.Embed(
        title="🔄 /sync discord-rollback",
        description=(
            "**Cílový sync:**\n"
            f"• Čas: **{_fmt_ts(plan['target_ts'])}** (`{plan['target_ts']}`)\n"
            f"• Kdo: **{plan['target_actor_name']}** (ID `{plan['target_actor_id']}`)\n"
            f"• Úspěšně aplikováno: **{plan['original_applied']} / "
            f"{plan['total_logged']}** akcí\n\n"
            "**Přehled dle původní operace auditu:**\n"
            f"• Original REMOVE → Rollback ADD: **{remove_to_add}**\n"
            f"• Original ADD → Rollback REMOVE: **{add_to_remove}**\n"
            f"• Celkem: **{total}** · kontrola X + Y = celkem: "
            f"**{'OK ✓' if sum_ok else 'NESHODA ✗'}**\n\n"
            f"**Finální bezpečnostní ověření:** {verified} / {total} akcí "
            f"(memberId + roleId + op + důkaz ok=True) "
            f"{'✓' if verified_ok else '✗'}"
        ),
        color=0xF59E0B,
    )
    embed.set_footer(text=footer)
    _append_note(embed, note)
    return [embed]


def _websync_embed(result: dict, *, mode: str, note: str = "") -> list:
    """Embed pack s přehledem rozdílů / výsledkem (preview / apply)."""
    if not result["ok"]:
        embed = discord.Embed(
            title="⚠️ /sync web – nelze pokračovat",
            description=result["message"],
            color=0xEF4444,
        )
        if mode == "apply":
            embed.set_footer(text="Nic se na web neposílalo.")
        _append_note(embed, note)
        return [embed]

    analysis = result["analysis"] or {}
    if analysis.get("has_issues"):
        sections = [
            ("Rozdíly oproti webu", [f["message"] for f in analysis["findings"]])
        ]
        if analysis.get("website_only"):
            extra = [str(u) for u in analysis["website_only"]]
            sections.append(
                (
                    "Hráči jen na webu (ne v kanonické DB)",
                    [
                        "Synchronizace web přepíše kanonickou DB – tito hráči "
                        "z webu zmizí: " + ", ".join(extra)
                    ],
                )
            )
        if mode == "apply":
            footer = (
                f"Synchronizace NAHRADÍ players.json na webu kanonickou DB "
                f"({analysis['canonical_count']} záznamů) – potvrď tlačítkem níže."
            )
        else:
            footer = (
                "Náhled – nic se neposílalo. Pro potvrzení použij "
                "/sync web mode:apply."
            )
        embeds = build_embed_pack(
            title=(
                f"🔎 /sync web – "
                f"{'potvrzení synchronizace' if mode == 'apply' else 'náhled'}"
            ),
            description=(
                f"Kanonická DB: **{analysis['canonical_count']}** hráčů · "
                f"Web: **{analysis['website_count']}** hráčů\n\n"
                + "\n".join(
                    f"{WS_KIND_LABELS[k]}: **{analysis['summary'].get(k, 0)}**"
                    for k in WS_KINDS
                )
            ),
            color=0xF59E0B,
            footer=footer,
            sections=sections,
        )
        _append_note(embeds[0], note)
        return embeds

    embed = discord.Embed(
        title="✅ /sync web – web je v synchronizaci",
        description=(
            f"Kanonická DB (**{analysis['canonical_count']}** hráčů) odpovídá "
            "webu – nemám co opravovat."
        ),
        color=0x10B981,
    )
    _append_note(embed, note)
    return [embed]


def _datacheck_embed(report: dict, *, note: str = "") -> list:
    """Embed pack /sync data – VŠECHNY nálezy (rozdělené, nezkrácené)."""
    if not report["has_issues"]:
        embed = discord.Embed(
            title="✅ /sync data – vše v pořádku",
            description="Všechny databáze jsou konzistentní – nemám co hlásit.",
            color=0x10B981,
        )
        _append_note(embed, note)
        return [embed]

    summary_text = "\n".join(
        f"{DC_KIND_LABELS[k]}: **{report['summary'].get(k, 0)}**" for k in DC_KINDS
    ) or "_žádné nálezy_"
    sections = [
        (
            f"Nálezů celkem: {report['total_findings']}",
            [f["message"] for f in report["findings"]],
        )
    ]

    r = report["repairable"]
    if report["repairable_count"]:
        parts = []
        if r["close_ticket"]:
            parts.append(f"zavřít **{len(r['close_ticket'])}** osamocených ticketů")
        if r["normalize_tier"]:
            parts.append(f"normalizovat **{len(r['normalize_tier'])}** tierů")
        sections.append(
            (
                "🔧 Bezpečné opravy (nic se nemaže)",
                [
                    f"Po potvrzení tlačítkem: {', '.join(parts)}. "
                    "Záznamy zůstávají, audit se zapíše."
                ],
            )
        )
    embeds = build_embed_pack(
        title="🔍 /sync data – kontrola integrity",
        description=summary_text,
        color=0xEF4444,
        footer=(
            "Nic se nemění automaticky – opravy jen po potvrzení. "
            f"Audit: data/{DATACHECK_LOG_FILE}."
        ),
        sections=sections,
    )
    _append_note(embeds[0], note)
    return embeds


def _repair_result_embed(result: dict) -> list:
    """Embed pack s výsledkem bezpečných oprav (chyby VŠECHNY, nezkrácené)."""
    ok = bool(result.get("ok"))
    sections = []
    if result["errors"]:
        sections.append(("Chyby", [str(e) for e in result["errors"]]))
    return build_embed_pack(
        title="🔧 /sync data – bezpečné opravy",
        description=(
            f"{result['message']}\n"
            f"- zavřeno ticketů: **{len(result['closed'])}** "
            f"(z toho {sum(1 for c in result['closed'] if c.get('skipped'))} už zavřených)\n"
            f"- normalizováno tierů: **{len(result['normalized'])}**\n"
            f"- chyb: **{len(result['errors'])}**"
        ),
        color=0x10B981 if ok else 0xEF4444,
        footer=f"Zapsáno do data/{DATACHECK_LOG_FILE} (audit).",
        sections=sections,
    )


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


def _checkweb_embed(analysis: dict, *, mode: str, note: str = "") -> list:
    """Embed pack s přehledem (preview / apply) – VŠECHNY nálezy."""
    records = analysis["records"]
    summary = analysis["summary"]
    source = analysis.get("website_source", "")

    if not records:
        embeds = [
            discord.Embed(
                title="✅ /checkweb – vše v pořádku",
                description=(
                    "Discord role odpovídají players.json a webu – nemám co "
                    f"opravovat.\nWeb: {source}."
                ),
                color=0x10B981,
            )
        ]
    else:
        summary_text = "\n".join(
            f"{STATUS_LABELS[s]}: **{summary.get(s, 0)}**" for s in STATUSES
        )
        sections = [
            (
                "Nálezy",
                [_checkweb_compact(r) for r in records if r["status"] != "MATCH"],
            )
        ]
        if summary.get("MATCH"):
            sections.append(
                (
                    "✅ Shoda",
                    [
                        f"**{summary['MATCH']}** záznamů odpovídá ve všech "
                        "zdrojích."
                    ],
                )
            )
        embeds = build_embed_pack(
            title=f"🔎 /checkweb – {'potvrzení' if mode == 'apply' else 'náhled'}",
            description=(
                f"Zkontrolováno záznamů (hráč × kit): **{analysis['checked']}**\n"
                f"Web: {source}\n\n"
                + summary_text
            ),
            color=0xF59E0B,
            sections=sections,
        )

    if mode == "apply":
        resolvable = analysis.get("resolvable") or []
        if resolvable:
            footer = (
                f"{len(resolvable)} záznamů k řešení – vyber rozhodnutí "
                "a potvrď tlačítkem. Web se nemění (na to je /sync web)."
            )
        else:
            footer = (
                "Žádný záznam nevyžaduje rozhodnutí – opravy jsou ruční "
                "(viz nálezy) nebo přes /sync web apply."
            )
    else:
        footer = (
            "Náhled – žádné změny neaplikovány. JEDINÝ zapisovatel je "
            "/checkweb apply s potvrzením."
        )
    embeds[0].set_footer(text=footer)
    _append_note(embeds[0], note)
    return embeds


def _check_embed(
    items: list,
    *,
    counts: dict,
    website_source: str,
    area: str,
    corrupt: list,
    repairable_count: int,
    note: str = "",
) -> list:
    """Embed pack /sync check – nálezy podle severity, VŠECHNY bez zkrácení."""
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
        return [embed]

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
        return [embed]

    severity_lines = "\n".join(
        f"{SEVERITY_LABELS[s]}: **{counts.get(s, 0)}**" for s in SEVERITY_ORDER
    )
    title_suffix = f" ({AREA_LABELS.get(area, area)})" if area != "all" else ""
    sections = []
    for sev in SEVERITY_ORDER:
        group = [i for i in items if i["severity"] == sev]
        if not group:
            continue
        sections.append(
            (f"{SEVERITY_LABELS[sev]} ({len(group)})", [i["message"] for i in group])
        )
    if repairable_count:
        sections.append(
            (
                "🔧 Bezpečné opravy",
                [
                    f"{repairable_count} oprav je dostupných přes **/sync data** "
                    "(potvrzení tlačítkem)."
                ],
            )
        )
    embeds = build_embed_pack(
        title=f"🔎 /sync check – náhled stavu{title_suffix}",
        description=(
            f"Zdroj dat webu: **{website_source}**\nNálezů celkem: **{total}**\n\n"
            + severity_lines
        ),
        color=0xEF4444 if counts.get("error") else 0xF59E0B,
        footer=(
            "Read-only – nic se nemění. Audit: data/checkweb_log.json "
            "a data/datacheck_log.json."
        ),
        sections=sections,
    )
    _append_note(embeds[0], note)
    return embeds


# ---------------------------------------------------------------------------
# Potvrzovací view (přesunuto beze změny logiky ze starých cogů)
# ---------------------------------------------------------------------------
class SyncDiscordRollbackView(SafeView):
    """Tlačítko „Potvrdit a vrátit změny" pro /sync discord-rollback apply.

    Před spuštěním znovu ověří cílový auditní záznam + otisk plánu – změnil-li
    se mezi náhledem a potvrzením, rollback se NESPUSTÍ (stale). Po dokončení
    zapíše rollback audit do data/playersync_rollback_log.json.
    """

    def __init__(self, *, plan: dict):
        super().__init__(timeout=120)
        self.plan = plan
        self.fingerprint = fingerprint(plan["actions"])
        self.finished = False

    @discord.ui.button(
        label="↩️ Potvrdit a vrátit změny",
        style=discord.ButtonStyle.danger,
        custom_id="sync_discord_rollback_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.finished:
            return await interaction.response.send_message(
                "✅ Rollback už proběhl.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        # 1) Ověření, že se audit od náhledu nezměnil → vrátíme PŘESNĚ to,
        #    co admin potvrdil (nikdy nic automaticky navíc).
        entries = await get_playersync_log()
        fresh_entry, _warnings = find_rollback_target(
            entries, target_ts=self.plan["target_ts"]
        )
        if fresh_entry is None:
            self.finished = True
            await self._finish(interaction, results=None, stale=True)
            return
        fresh_plan = build_rollback_plan(fresh_entry)
        fresh_verify = verify_rollback_plan(fresh_entry, fresh_plan)
        if (
            not fresh_plan["ok"]
            or fingerprint(fresh_plan["actions"]) != self.fingerprint
            or not fresh_verify["ok"]
            or not fresh_verify["sum_matches"]
        ):
            self.finished = True
            await self._finish(interaction, results=None, stale=True)
            return

        # 2) Aplikace rollback akcí – každá zvlášť, chyby se nešíří dál.
        #    H3 audit fix: session_factory/target_ts umožní za-akci ověřit,
        #    že hráč nebyl po cíleném syncu znovu povýšen přes /result
        #    (jinak se akce přeskočí, viz apply_rollback_actions).
        results = await apply_rollback_actions(
            interaction.guild,
            fresh_plan["actions"],
            session_factory=getattr(interaction.client, "db_session_factory", None),
            target_ts=fresh_plan["target_ts"],
        )

        # 3) Rollback audit (separátní soubor; původní audit syncu se nemění).
        summary = {
            "applied": sum(1 for r in results if r.get("status") == "applied"),
            "already_correct": sum(
                1 for r in results if r.get("status") == "already_correct"
            ),
            "failed": sum(1 for r in results if r.get("status") == "failed"),
            "skipped_newer_promotion": sum(
                1 for r in results if r.get("status") == "skipped_newer_promotion"
            ),
            "total": len(results),
        }
        try:
            await log_playersync_rollback_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="apply",
                target_ts=fresh_plan["target_ts"],
                target_actor_id=fresh_plan["target_actor_id"],
                target_actor_name=fresh_plan["target_actor_name"],
                original_applied=fresh_plan["original_applied"],
                total_logged=fresh_plan["total_logged"],
                results=results,
                summary=summary,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit aplikaci
            log.exception("Rollback audit (apply) selhal")

        self.finished = True
        await self._finish(interaction, results=results, stale=False)

    async def _finish(self, interaction, *, results, stale) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /sync discord-rollback – audit se změnil",
                description=(
                    "Mezitím se změnil cílový auditní záznam – **nic jsem "
                    "nevrátil**. Spusť **/sync discord-rollback mode:apply** "
                    "znovu."
                ),
                color=0xEF4444,
            )
        else:
            applied = sum(1 for r in results if r.get("status") == "applied")
            already = sum(
                1 for r in results if r.get("status") == "already_correct"
            )
            failed = sum(1 for r in results if r.get("status") == "failed")
            skipped_newer = sum(
                1 for r in results if r.get("status") == "skipped_newer_promotion"
            )
            lines = []
            for r in results[:15]:
                mark = {
                    "applied": "✅",
                    "already_correct": "🟢",
                    "failed": "❌",
                    "skipped_newer_promotion": "⏭️",
                }.get(r.get("status"), "❓")
                op = "přidána" if r.get("op") == "add" else "odebrána"
                line = (
                    f"{mark} <@{r.get('memberId')}> – {op} role "
                    f"<@&{r.get('roleId')}>"
                )
                if r.get("status") == "failed":
                    line += f" (chyba: {r.get('error')})"
                elif r.get("status") == "already_correct":
                    line += " (už ve stavu po rollbacku)"
                elif r.get("status") == "skipped_newer_promotion":
                    line += f" ({r.get('error')})"
                lines.append(line)
            if len(results) > 15:
                lines.append(f"…a dalších {len(results) - 15} akcí")
            embed = discord.Embed(
                title="↩️ /sync discord-rollback – dokončeno",
                description=(
                    f"Aplikováno: **{applied}** · Už správně: **{already}** · "
                    f"Přeskočeno (novější /result): **{skipped_newer}** · "
                    f"Chyby: **{failed}** / {len(results)}\n\n"
                    + "\n".join(lines)
                ),
                color=(
                    0x10B981
                    if failed == 0 and results
                    else 0xEF4444
                    if failed
                    else 0xF59E0B
                ),
            )
            embed.set_footer(
                text="Zapsáno do data/playersync_rollback_log.json (audit)."
            )

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
        session_factory = getattr(interaction.client, "db_session_factory", None)
        canonical = await _canonical_for_export(session_factory)
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


class SyncImportDiscordConfirmView(SafeView):
    """Potvrdí bezpečný hromadný import Discord tierů do DB a na web."""

    def __init__(self, *, guild, analysis: dict):
        super().__init__(timeout=120)
        self.guild = guild
        self.fingerprint = analysis["fingerprint"]
        self.finished = False

    @discord.ui.button(
        label="ℹ️ Jak to opravit",
        style=discord.ButtonStyle.success,
        custom_id="sync_importdiscord_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.finished:
            return await interaction.response.send_message(
                "✅ Import už proběhl.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        fresh = await _gather_checkweb(
            self.guild,
            session_factory=getattr(
                interaction.client, "db_session_factory", None
            ),
        )
        if fingerprint_resolvable(fresh["records"]) != self.fingerprint:
            self.finished = True
            return await self._finish(interaction, stale=True)

        decisions, skipped = build_discord_import_decisions(fresh["records"])
        # F-FIX: players.json se už nezapisuje. `apply_checkweb_decisions` jen
        # spočítá návrh; current tier promítne do PostgreSQL /sync discord
        # (Discord → PG mirror) a players.json obnoví canonical export.
        _, applied = apply_checkweb_decisions(
            players=await _canonical_players(
                getattr(interaction.client, "db_session_factory", None)
            ),
            records=fresh["records"],
            decisions=decisions,
            kit_display=fresh.get("kit_display") or {},
        )

        try:
            await log_checkweb_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="import_discord",
                status="superseded",
                summary=fresh["summary"],
                website=fresh.get("website_source"),
                errors=(
                    "F-FIX: zápis players.json odstraněn – "
                    "použij /sync discord (Discord → PostgreSQL mirror)"
                ),
                repairs=applied,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit výpis
            log.exception("Auditní zápis (/sync importdiscord) selhal")

        self.finished = True
        await self._finish(interaction, applied=applied, skipped=skipped)

    async def _finish(
        self, interaction, *, stale=False, applied=None, skipped=None
    ) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /sync importdiscord – stav se změnil",
                description=(
                    "Mezitím se změnily Discord role nebo PostgreSQL mirror – "
                    "**nic jsem nezapsal**. Spusť příkaz znovu."
                ),
                color=0xEF4444,
            )
        else:
            changed = [
                a for a in (applied or [])
                if a.get("ok") and a.get("newTier") != a.get("oldTier")
            ]
            lines = [
                f"✏️ **{a['player']}** · **{a['kit']}**: "
                f"{a.get('oldTier') or '—'} → **{a.get('newTier')}**"
                for a in changed[:12]
            ]
            if len(changed) > 12:
                lines.append(f"…a dalších {len(changed) - 12} změn")
            description = (
                f"Rozhodnutí k promítnutí: **{len(changed)}** tierů. "
                f"Přeskočeno k ručnímu řešení: **{len(skipped or [])}**.\n\n"
                + ("\n".join(lines) if lines else "Žádná jednoznačná změna.")
                + "\n\n**Nic jsem nezapsal.** Tento příkaz už players.json ani "
                "PostgreSQL nemění. Použij `/sync discord` (Discord → "
                "PostgreSQL mirror) a pak `/sync web` (export na web)."
            )
            embed = discord.Embed(
                title="ℹ️ /sync importdiscord – jen náhled",
                description=description,
                color=0xF59E0B,
            )
            embed.set_footer(text="Audit: data/checkweb_log.json")
        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit potvrzení /sync importdiscord: %s", err)
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
            embeds = [
                discord.Embed(
                    title="❌ /sync data – poškozená data",
                    description=(
                        "Opravy se NEPROVEDLY – soubor není platný JSON "
                        f"(`{err}`). Poškozené soubory se nikdy automaticky "
                        "nepřepisují."
                    ),
                    color=0xEF4444,
                )
            ]
            await _edit_embed_pack(interaction, embeds)
            await _send_embed_pack(interaction.followup, embeds)
            return

        r = report["repairable"]
        db_mode = getattr(interaction.client, "db_session_factory", None) is not None
        if db_mode:
            # F-FIX: players.json je v DB režimu generovaný export – normalizovat
            # v něm `modes` by znamenalo zápis current tieru do neautority.
            r = {**r, "normalize_tier": []}
        if not r["close_ticket"] and not r["normalize_tier"]:
            embeds = [
                discord.Embed(
                    title="✅ /sync data – už není co opravit",
                    description=(
                        "Kontrola po náhledu nehlásí žádné bezpečné opravy."
                        + (
                            "\n\nV PostgreSQL režimu se navíc neopravují tiery v "
                            "players.json – current tier je v `player_current_tiers` "
                            "a opravuje se přes `/edituser`."
                            if db_mode
                            else ""
                        )
                    ),
                    color=0x10B981,
                )
            ]
        else:
            try:
                result = await perform_repairs(
                    close_ticket_ids=r["close_ticket"],
                    tier_fixes=r["normalize_tier"],
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                )
            except DataCorruptionError as err:
                embeds = [
                    discord.Embed(
                        title="❌ /sync data – poškozená data",
                        description=(
                            "Opravy se NEPROVEDLY – soubor není platný JSON "
                            f"(`{err}`). Poškozené soubory se nikdy automaticky "
                            "nepřepisují."
                        ),
                        color=0xEF4444,
                    )
                ]
            else:
                embeds = _repair_result_embed(result)

        await _edit_embed_pack(interaction, embeds)
        await _send_embed_pack(interaction.followup, embeds)


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
        fresh = await _gather_checkweb(
            self.guild,
            session_factory=getattr(
                interaction.client, "db_session_factory", None
            ),
        )
        if fingerprint_resolvable(fresh["records"]) != self.fingerprint:
            self.finished = True
            await self._finish(interaction, applied=None, stale=True)
            return

        # 2) Rozhodnutí se pouze SPOČÍTAJÍ jako návrh. F-FIX: players.json ani
        #    JSONB se už nezapisují – current tier promítne do PostgreSQL
        #    /sync discord (Discord → PG mirror).
        _, applied = apply_checkweb_decisions(
            players=await _canonical_players(
                getattr(interaction.client, "db_session_factory", None)
            ),
            records=fresh["records"],
            decisions=list(self.decisions.values()),
            kit_display=fresh.get("kit_display") or {},
        )

        # 3) Audit – KAŽDÉ rozhodnutí (i keep/ignore) se zapisuje.
        try:
            await log_checkweb_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="apply",
                status="superseded",
                summary=fresh["summary"],
                website=fresh.get("website_source"),
                errors=(
                    "F-FIX: zápis players.json odstraněn – "
                    "použij /sync discord (Discord → PostgreSQL mirror)"
                ),
                repairs=applied,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit výpis
            log.exception("Auditní zápis (/checkweb apply) selhal")

        self.finished = True
        await self._finish(interaction, applied=applied, stale=False)

    async def _finish(self, interaction, *, applied, stale) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /checkweb – stav se změnil",
                description=(
                    "Mezitím se změnily role, web nebo PostgreSQL mirror – "
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
                title="ℹ️ /checkweb – rozhodnutí pouze zaznamenána",
                description=(
                    f"Rozhodnutí k promítnutí: **{ok} / {total}**\n\n"
                    + "\n".join(lines)
                    + "\n\n**Nic jsem nezapsal.** Tento příkaz už players.json ani "
                    "PostgreSQL nemění. Použij `/sync discord` (Discord → "
                    "PostgreSQL mirror) a pak `/sync web` (export na web)."
                ),
                color=0xF59E0B,
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
            embeds = _check_embed(
                [], counts={"ok": 0, "warning": 0, "conflict": 0, "error": 0},
                website_source="n/a", area=area, corrupt=corrupt,
                repairable_count=0, note=note,
            )
            await _send_embed_pack(interaction.followup, embeds)
            return

        players = await _canonical_players(
            getattr(interaction.client, "db_session_factory", None)
        )
        roles_map = await get_all_kit_role_maps(
            session_factory=getattr(interaction.client, "db_session_factory", None)
        )
        kit_display = await kit_display_map(
            session_factory=getattr(
                interaction.client, "db_session_factory", None
            )
        )
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

        embeds = _check_embed(
            items,
            counts=counts,
            website_source=website_source,
            area=area,
            corrupt=[],
            repairable_count=dc["repairable_count"],
            note=note,
        )
        await _send_embed_pack(interaction.followup, embeds)

    # ------------------------------------------------------------------
    # /sync discord (observe-only: Discord → PostgreSQL mirror)
    # ------------------------------------------------------------------
    @sync.command(
        name="discord",
        description="Observe-only: Discord tier role → PostgreSQL mirror",
    )
    async def sync_discord(self, interaction: discord.Interaction) -> None:
        await self._run_discord(interaction)

    async def _run_discord(
        self, interaction: discord.Interaction, *, note: str = ""
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        session_factory = getattr(self.bot, "db_session_factory", None)
        if session_factory is None:
            embed = discord.Embed(
                title="❌ /sync discord – PostgreSQL není nakonfigurováno",
                description=(
                    "/sync discord vyžaduje DATABASE_URL. Bez PostgreSQL "
                    "nelze mirror zapisovat – **nic se neměnilo** (ani mirror, "
                    "ani Discord role)."
                ),
                color=0xEF4444,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        members = await guild_members(interaction.guild)
        try:
            outcome = await DiscordSyncService().sync_guild(
                session_factory,
                members=members,
                triggered_by=interaction.user.id,
                triggered_by_name=str(interaction.user),
            )
        except Exception as err:  # noqa: BLE001 – selhání DB = tvrdá chyba
            log.exception("Selhání /sync discord (PostgreSQL)")
            embed = discord.Embed(
                title="❌ /sync discord – databáze selhala",
                description=(
                    f"Mirror se NEzapsal (`{err}`). Discord role se vůbec "
                    "neměnily – jsou i nadále jedinou autoritou. Zkuste to "
                    "znovu; přetrvává-li problém, zkontrolujte PostgreSQL."
                ),
                color=0xEF4444,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embeds = await self._discord_observe_embed(
            interaction, session_factory, outcome, note=note
        )
        await _send_embed_pack(interaction.followup, embeds)

    async def _discord_observe_embed(
        self,
        interaction: discord.Interaction,
        session_factory,
        outcome: "DiscordSyncOutcome",
        *,
        note: str = "",
    ) -> list:
        """Embed z výsledku observe-only syncu (per-člen detaily ze sync_actions)."""
        try:
            async with transaction(session_factory) as session:
                actions = await SyncActionRepository().list_for_run(
                    session, sync_run_id=outcome.sync_run_id
                )
        except Exception:  # noqa: BLE001 – výpis nesmí spadnout kvůli detailům
            log.exception("Čtení sync_actions pro embed selhalo")
            actions = []

        applied_lines, anomaly_lines, failed_lines = [], [], []
        for a in actions:
            member = f"<@{a.member_id}>" if a.member_id is not None else "_?_"
            if a.status == SYNC_ACTION_APPLIED and a.discord_role_id is not None:
                applied_lines.append(f"{member} → <@&{a.discord_role_id}>")
            elif a.status == SYNC_ACTION_APPLIED:
                applied_lines.append(f"{member} → tier `{a.tier_id}`")
            elif a.status == SYNC_ACTION_FAILED:
                err = (a.details or {}).get("error", "?")
                failed_lines.append(f"{member} · `{err}`")
            else:
                cat = a.anomaly_category or a.action_type
                anomaly_lines.append(f"{member} · `{cat}`")

        status_icon = {
            SYNC_RUN_SUCCESS: "✅",
            SYNC_RUN_PARTIAL: "⚠️",
            SYNC_RUN_FAILED: "❌",
        }.get(outcome.status, "❓")
        unknown_roles = (
            ", ".join(f"`{r}`" for r in outcome.unknown_roles)
            if outcome.unknown_roles
            else "**0**"
        )
        description = (
            f"Prozkoumáno členů: **{outcome.scanned_members}**\n"
            f"Zrcadleno pozorování: **{outcome.observations_applied}**\n"
            f"Anomálií: **{outcome.anomalies}** · Neznámých hráčů: "
            f"**{outcome.unknown_members}** · Neznámých rolí: {unknown_roles}\n"
            f"Chyb: **{outcome.failed_members}**"
        )
        sections = []
        if applied_lines:
            sections.append(("Zrcadleno do PostgreSQL", applied_lines))
        if anomaly_lines:
            sections.append(("⚠️ Anomálie (neopravují se automaticky)", anomaly_lines))
        if failed_lines:
            sections.append(("❌ Chyby zpracování", failed_lines))

        embeds = build_embed_pack(
            title=f"{status_icon} /sync discord – Discord → PostgreSQL mirror",
            description=description,
            color=(
                0x10B981
                if outcome.status == SYNC_RUN_SUCCESS
                else 0xF59E0B
                if outcome.status == SYNC_RUN_PARTIAL
                else 0xEF4444
            ),
            footer=(
                "Observe-only – Discord role se NEMĚNÍ. Discord je jediná "
                f"autorita aktuálních tierů. Run: #{outcome.sync_run_id}."
            ),
            sections=sections,
        )
        _append_note(embeds[0], note)
        return embeds

    # ------------------------------------------------------------------
    # /sync discord-rollback
    # ------------------------------------------------------------------
    @sync.command(
        name="discord-rollback",
        description="Vrátí poslední aplikovaný /sync discord (dry run defaultně)",
    )
    @app_commands.describe(
        mode="preview = dry run (nic nemění) · apply = po potvrzení vrátí",
        target_ts="ts cílového syncu v ms (výchozí: poslední aplikovaný)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="preview", value="preview"),
            app_commands.Choice(name="apply", value="apply"),
        ]
    )
    async def sync_discord_rollback(
        self,
        interaction: discord.Interaction,
        mode: str = "preview",
        target_ts: int | None = None,
    ) -> None:
        await self._run_discord_rollback(
            interaction, mode=mode, target_ts=target_ts
        )

    async def _run_discord_rollback(
        self,
        interaction: discord.Interaction,
        mode: str = "preview",
        target_ts: int | None = None,
        *,
        note: str = "",
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        # 1) Cílový aplikovaný sync z auditu (nikdy preview / bez applied).
        entries = await get_playersync_log()
        entry, warnings = find_rollback_target(entries, target_ts=target_ts)
        if entry is None:
            embed = discord.Embed(
                title="❌ /sync discord-rollback – nelze",
                description="\n".join(warnings or ["Žádný vhodný záznam."]),
                color=0xEF4444,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 2) Invertovaný plán – chybějící členId/roleId = bezpečný abort.
        plan = build_rollback_plan(entry)
        if not plan["ok"]:
            missing_lines = [
                f"• member `{m.get('memberId')}` · role `{m.get('roleId')}` "
                f"({m.get('reason')})"
                for m in plan["missing"]
            ]
            embed = discord.Embed(
                title="❌ /sync discord-rollback – chybí informace",
                description=(
                    "Rollback se NESPUSTÍ, dokud cílový audit neobsahuje "
                    "memberId a roleId pro každou akci:\n\n"
                    + "\n".join(missing_lines)
                ),
                color=0xEF4444,
            )
            embed.set_footer(
                text=f"Cílový sync: {_fmt_ts(plan['target_ts'])} "
                f"({plan['target_ts']})."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not plan["actions"]:
            embed = discord.Embed(
                title="ℹ️ /sync discord-rollback – není co vracet",
                description=(
                    "Cílový sync nemá žádné úspěšně aplikované akce "
                    "(či všechny selhaly)."
                ),
                color=0x10B981,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 3) Finální bezpečnostní kontrola – každá akce plánu se ověří proti
        #    audit záznamu (memberId + roleId + op + důkaz ok=True) a souhrn
        #    dle původní operace musí dát X + Y == total. Při neúspěchu se
        #    rollback NESPUSTÍ – žádný výstup s potvrzením, žádné volání API.
        verification = verify_rollback_plan(entry, plan)
        if not verification["ok"] or not verification["sum_matches"]:
            problem_lines = [
                f"• member `{p.get('member_id')}` · role `{p.get('role_id')}` "
                f"(original {p.get('original_op')}) – {p.get('reason')}"
                for p in verification["problems"]
            ]
            embed = discord.Embed(
                title="❌ /sync discord-rollback – bezpečnostní kontrola selhala",
                description=(
                    "Rollback se NESPUSTÍ – plán neprošel finálním ověřením "
                    "proti auditu (chybí memberId/roleId nebo důkaz ok=True, "
                    "případně špatná inverze):\n\n"
                    + "\n".join(problem_lines or ["Neznámá chyba ověření."])
                ),
                color=0xEF4444,
            )
            embed.set_footer(
                text=f"Cílový sync: {_fmt_ts(plan['target_ts'])} "
                f"({plan['target_ts']})."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 4) Rollback audit (preview i apply – dry run se taky zaznamenává).
        summary = {
            "add": sum(1 for a in plan["actions"] if a.get("op") == "add"),
            "remove": sum(1 for a in plan["actions"] if a.get("op") == "remove"),
            "total": len(plan["actions"]),
        }
        try:
            await log_playersync_rollback_event(
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
                mode="preview",
                target_ts=plan["target_ts"],
                target_actor_id=plan["target_actor_id"],
                target_actor_name=plan["target_actor_name"],
                original_applied=plan["original_applied"],
                total_logged=plan["total_logged"],
                plan=plan["actions"],
                summary=summary,
            )
        except Exception:  # noqa: BLE001 – audit nesmí shodit výpis
            log.exception("Rollback audit (preview) selhal")

        embeds = _rollback_embed(
            plan, mode=mode, note=note, verification=verification
        )
        if mode != "apply":
            # Dry run – NIKDY nemění Discord.
            await _send_embed_pack(interaction.followup, embeds)
            return

        view = SyncDiscordRollbackView(plan=plan)
        await _send_embed_pack(interaction.followup, embeds, view=view)

    # ------------------------------------------------------------------
    # /sync importdiscord (Discord → DB → web)
    # ------------------------------------------------------------------
    @sync.command(
        name="importdiscord",
        description="Náhled jednoznačných Discord tierů proti PostgreSQL mirroru (nemění data)",
    )
    @app_commands.describe(mode="preview = náhled · apply = jen zopakuje, co by /sync discord změnil")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="preview", value="preview"),
            app_commands.Choice(name="apply", value="apply"),
        ]
    )
    async def sync_importdiscord(
        self, interaction: discord.Interaction, mode: str
    ) -> None:
        """Hromadný import tier rolí, pouze pro jednoznačně spárované hráče.

        Discord neumí spolehlivě dodat IGN nového člena a více tier rolí je
        konflikt. Tyto případy se proto nikdy nevytvářejí ani nevolí samy.
        """
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        analysis = await _gather_checkweb(
            interaction.guild,
            session_factory=getattr(
                self.bot, "db_session_factory", None
            ),
        )
        decisions, skipped = build_discord_import_decisions(analysis["records"])
        lines = [
            f"✏️ **{d['player']}** · **{d['kit_key']}** → **{d['tier']}**"
            for d in decisions[:15]
        ]
        if len(decisions) > 15:
            lines.append(f"…a dalších {len(decisions) - 15} změn")
        description = (
            f"Jednoznačné změny z Discordu: **{len(decisions)}**\n"
            f"Přeskočeno (konflikt, neznámý hráč, duplicita nebo chybějící role): "
            f"**{len(skipped)}**\n\n"
            + ("\n".join(lines) if lines else "Žádná bezpečná změna k importu.")
        )
        embed = discord.Embed(
            title="🔎 /sync importdiscord – náhled",
            description=description,
            color=0xF59E0B if decisions else 0x10B981,
        )
        embed.set_footer(
            text=(
                "Jen náhled – nic se nezapisuje. Změnu promítne "
                "/sync discord (Discord → PostgreSQL)."
                " Víc tier rolí se nikdy nevybírá automaticky."
            )
        )
        if mode == "preview" or not decisions:
            return await interaction.followup.send(embed=embed, ephemeral=True)

        view = SyncImportDiscordConfirmView(guild=interaction.guild, analysis=analysis)
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

        session_factory = getattr(getattr(self, "bot", None), "db_session_factory", None)
        canonical = await _canonical_for_export(session_factory)
        result = await preview_website(
            canonical=canonical,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        embeds = _websync_embed(result, mode=mode, note=note)

        if not result["ok"]:
            await _send_embed_pack(interaction.followup, embeds)
            return
        if not result["analysis"]["has_issues"] or mode != "apply":
            # preview = read-only náhled (věrné chování /websync): view jen
            # při mode:"apply" s rozdíly
            await _send_embed_pack(interaction.followup, embeds)
            return

        view = SyncWebConfirmView(
            canonical_fingerprint=result["fingerprint"]
        )
        await _send_embed_pack(interaction.followup, embeds, view=view)

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

        embeds = _datacheck_embed(report, note=note)
        if report["repairable_count"]:
            view = SyncDataRepairView()
            await _send_embed_pack(interaction.followup, embeds, view=view)
        else:
            await _send_embed_pack(interaction.followup, embeds)

    # ------------------------------------------------------------------
    # /checkweb apply – per-záznamová rozhodnutí (přesunuto beze změny)
    # ------------------------------------------------------------------
    async def _run_checkweb_apply(
        self, interaction: discord.Interaction, *, note: str = ""
    ) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        analysis = await _gather_checkweb(
            interaction.guild,
            session_factory=getattr(
                self.bot, "db_session_factory", None
            ),
        )
        embeds = _checkweb_embed(analysis, mode="apply", note=note)

        resolvable = analysis.get("resolvable") or []
        if not resolvable:
            await _send_embed_pack(interaction.followup, embeds)
            return

        view = CheckWebApplyView(
            guild=interaction.guild,
            records=resolvable,
            fingerprint=analysis["fingerprint"],
        )
        await _send_embed_pack(interaction.followup, embeds, view=view)

    # ------------------------------------------------------------------
    # Deprecated aliasy (funkční, chovají se identicky jako /sync)
    # ------------------------------------------------------------------
    playersync = app_commands.Group(
        name="playersync",
        description="DEPRECATED – použij /sync discord",
    )

    @playersync.command(
        name="preview",
        description="DEPRECATED – použij /sync discord",
    )
    async def playersync_preview(self, interaction: discord.Interaction) -> None:
        await self._run_discord(
            interaction,
            note="⚠️ Deprecated – použij /sync discord.",
        )

    @playersync.command(
        name="apply",
        description="DEPRECATED – /sync discord je observe-only (role se nemění)",
    )
    async def playersync_apply(self, interaction: discord.Interaction) -> None:
        await self._run_discord(
            interaction,
            note="⚠️ Deprecated – použij /sync discord.",
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
                "⚠️ Deprecated – explicitní Discord→DB zapisovač. "
                "Preferuje se /edituser (nebo /sync discord mirror)."
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

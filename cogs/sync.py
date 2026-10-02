"""Centrální synchronizace – /sync (discord | web | check | rollback).

Discord je JEDINÁ autorita aktuálních tier rolí:

    Discord tier role → PostgreSQL mirror  (/sync discord, DiscordSyncService)
    PostgreSQL        → web/GitHub         (/sync web, services/websync)
    /result /topresult /linkdiscord        (explicitní operace s rolemi)

- ``/sync discord``   – JEDINÁ vstupní brána Discord → PostgreSQL. Stáhne členy,
                        porovná tier role s DB mirrorem (dry run), ukáže změny
                        a anomálie a po potvrzení tlačítkem zapíše mirror.
                        Nikdy nemění Discord role a nikdy neopravuje anomálie
                        automaticky.
- ``/sync web``       – preview/apply: DB → web. Selhání GitHubu NIKDY není
                        hlášeno jako úspěch; prázdná DB se na web neposílá.
- ``/sync check``     – read-only diagnostika: Discord × PostgreSQL × web
                        + health mirror. Nález se rozdělí na OK / WARNING /
                        CONFLICT / ERROR a dá se filtrovat podle oblasti (area).
- ``/sync rollback``  – jediná mutace rolí normálního syncu: invertuje PŘESNĚ
                        akce historického aplikovaného syncu z auditu
                        (obnova po chybném odsouhlasení; jinak se role nemění).

Architektura: tento cog JE POUZE orchestrace. Business logika zůstává ve
službách (services/checkweb, services/playersync+role_sync, services/websync,
db/services/mirror_sync) – NEVYTVÁŘÍME žádné nové služby.
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
from db.repositories.sync_audit import SYNC_RUN_FAILED, SYNC_RUN_PARTIAL, SYNC_RUN_SUCCESS
from db.services import DiscordSyncOutcome, DiscordSyncService
from db.services.mirror_sync import (
    CHANGE_PLAYER_CREATED,
    CHANGE_TIER_ADDED,
    CHANGE_TIER_CHANGED,
)
from services.checkweb import analyze_checkweb, log_checkweb_event
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
from views import SafeView

log = logging.getLogger("dachshundtiers")

SYNC_MESSAGE = "websync: synchronizace hráčů na web"

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

AREAS = ("all", "identity", "roles", "web", "db")
AREA_LABELS = {
    "all": "Vše",
    "identity": "Identita",
    "roles": "Discord role",
    "web": "Web",
    "db": "PostgreSQL",
}


# ---------------------------------------------------------------------------
# Pomocné (čistá orchestrace – bez vlastní logiky)
# ---------------------------------------------------------------------------
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


# Web/GitHub export má JEDINÝ zdroj: PostgreSQL. ``data/players.json`` už
# není zdroj pravdy, a proto se nikdy nepoužije jako „náhrada", když export
# z DB selže. Selhání exportu je selhání exportu – operátor to musí vidět,
# jinak by na webu visel tichý rozestup, o kterém nikdo neví.
async def _canonical_for_export(session_factory) -> list[dict]:
    """Kanonická data hráčů pro web/GitHub export – výhradně z PostgreSQL.

    Dřív tady byl třetí zdroj pravdy: když export z DB selhal (nebo nebyla
    DB vůbec nakonfigurovaná), načetl se ``data/players.json`` a výsledek
    se prezentoval jako „synced", jen s oranžovou poznámkou. To je přesně
    situace, kdy se dvě pravdy tiš rozejdou a nikdo nepozná, která odpověď
    je správná. Teď výjimka propadne – volající ji musí ohlásit a export
    se vůbec neprovede.
    """
    if session_factory is None:
        raise RuntimeError(
            "export hráčů potřebuje PostgreSQL; players.json se už jako zdroj "
            "pravdy nepoužívá"
        )
    return await export_players(session_factory)


async def _canonical_players(session_factory) -> list:
    """Canonical data pro checkweb analýzu – výhradně z PostgreSQL.

    Výpadek DB vyvolá výjimku (analýza bez dat je bezcenná a klamavá), žádný
    tichý fallback na ``players.json``.
    """
    if session_factory is None:
        raise RuntimeError(
            "checkweb analýza potřebuje PostgreSQL; players.json se už "
            "nepoužívá jako zdroj current tieru"
        )
    return await export_players(session_factory)


async def _kit_role_maps_or_empty(session_factory) -> dict:
    """``get_all_kit_role_maps`` bez JSON fallbacku – bez DB vrátí {}."""
    if session_factory is None:
        return {}
    return await get_all_kit_role_maps(session_factory=session_factory)


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
        if cut <= len(prefix):
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
        title="🔄 /sync rollback",
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


def _check_embed(
    items: list,
    *,
    counts: dict,
    website_source: str,
    area: str,
    note: str = "",
) -> list:
    """Embed pack /sync check – nálezy podle severity, VŠECHNY bez zkrácení."""
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
            text="Read-only – nic se nemění. Audit: data/checkweb_log.json."
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
    embeds = build_embed_pack(
        title=f"🔎 /sync check – náhled stavu{title_suffix}",
        description=(
            f"Zdroj dat webu: **{website_source}**\nNálezů celkem: **{total}**\n\n"
            + severity_lines
        ),
        color=0xEF4444 if counts.get("error") else 0xF59E0B,
        footer="Read-only – nic se nemění. Audit: data/checkweb_log.json.",
        sections=sections,
    )
    _append_note(embeds[0], note)
    return embeds


HEALTH_COLORS = {
    "healthy": 0x10B981,
    "degraded": 0xF59E0B,
    "failed": 0xEF4444,
}
HEALTH_STATUS_LABELS = {
    "healthy": "🟢 healthy",
    "degraded": "🟡 degraded",
    "failed": "🔴 failed",
}
HEALTH_CHECK_LABELS = {
    "observation_freshness": "🕐 Freshness Discord pozorování",
    "last_mirror_sync": "🔁 Poslední úspěšný mirror sync",
    "unresolved_identities": "🧩 Nevyřešené importní identity",
    "sync_anomalies": "⚠️ Sync anomálie",
    "outbox_backlog": "📮 Outbox backlog",
    "outbox_stale_claims": "⏳ Rozbité (stale) outbox claimy",
    "unresolved_promotions": "🏆 Nevyřešená povýšení (discord_pending)",
    "recent_db_failures": "💥 Selhané DB operace (24 h)",
}


def _health_embed(report: dict) -> discord.Embed:
    """Embed s ``db.services.health.build_health_report`` — read-only.

    Tento report je jediný způsob, jak se operátor dozví o skutečně
    nevyřešených povýšeních (``unresolved_promotions``) a o mrtvém
    outboxu – obojí je dnes vidět jen přímo v DB. Je PŘÍMOČARÝ výstup
    jediné implementace, žádná duplikace logiky.
    """
    status = report.get("status", "failed")
    lines = []
    for check in report.get("checks", []):
        label = HEALTH_CHECK_LABELS.get(check["key"], check["label"])
        badge = HEALTH_STATUS_LABELS.get(check["status"], check["status"])
        lines.append(f"{badge} · **{label}** — {check['detail']}")
    embed = discord.Embed(
        title="🩺 /sync check — PostgreSQL mirror health",
        description="\n".join(lines) or "Žádné kontroly nebyly vráceny.",
        color=HEALTH_COLORS.get(status, 0xEF4444),
    )
    embed.set_footer(
        text=(
            f"Stav: {HEALTH_STATUS_LABELS.get(status, status)} · "
            f"{report.get('generated_at', '')} · read-only, nic se nemění"
        )
    )
    return embed


async def _db_health_embed(session_factory) -> list:
    """Health pack pro /sync check; prázdný list, když DB není nebo selhala.

    Selhání health reportu NESMÍ shodit celou `/sync check` – je to doplňková
    diagnostika, ne podmínka pro ostatní části výpisu.
    """
    if session_factory is None:
        return [
            discord.Embed(
                title="🩺 /sync check — PostgreSQL mirror health",
                description=(
                    "PostgreSQL není nakonfigurováno (`DATABASE_URL` chybí) – "
                    "mirror health nelze ověřit. Tento běh pracuje v legacy "
                    "JSON režimu."
                ),
                color=0xF59E0B,
            )
        ]
    try:
        from db.services.health import build_health_report

        return [_health_embed(await build_health_report(session_factory))]
    except Exception:  # noqa: BLE001 – health je doplňková diagnostika
        log.exception("PostgreSQL health report selhal")
        return [
            discord.Embed(
                title="🩺 /sync check — PostgreSQL mirror health",
                description=(
                    "Health report se nepodařilo sestavit – viz logy. Ostatní "
                    "části `/sync check` zůstávají platné."
                ),
                color=0xEF4444,
            )
        ]


def _discord_change_line(change) -> str:
    member = f"<@{change.member_id}>"
    kit = f" · **{change.kit_key}**" if change.kit_key else ""
    if change.kind == CHANGE_PLAYER_CREATED:
        return f"{member} · nový hráč `{change.detail}`"
    if change.kind == CHANGE_TIER_ADDED:
        return f"{member}{kit}: → **{change.new_tier}**"
    if change.kind == CHANGE_TIER_CHANGED:
        return f"{member}{kit}: {change.old_tier or '—'} → **{change.new_tier}**"
    if change.kind == "failed":
        return f"{member} · `{change.detail}`"
    return f"{member}{kit} · `{change.kind}`"


def _discord_embeds(
    outcome: DiscordSyncOutcome, *, preview: bool, note: str = ""
) -> list:
    """Embed pack /sync discord – náhled (dry run) i výsledek zápisu."""
    written, anomalies, failed = [], [], []
    for change in outcome.changes:
        if change.kind in (
            CHANGE_PLAYER_CREATED,
            CHANGE_TIER_ADDED,
            CHANGE_TIER_CHANGED,
        ):
            written.append(_discord_change_line(change))
        elif change.kind == "failed":
            failed.append(_discord_change_line(change))
        else:
            anomalies.append(_discord_change_line(change))

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
        f"{'K zápisu' if preview else 'Zapsáno'} změn tierů: "
        f"**{outcome.tier_changes}**\n"
        f"Anomálií: **{outcome.anomalies}** · Neznámých hráčů: "
        f"**{outcome.unknown_members}** · Neznámých rolí: {unknown_roles}\n"
        f"Chyb: **{outcome.failed_members}**"
    )
    sections = []
    if written:
        sections.append(
            ("K zápisu do PostgreSQL" if preview else "Zapsáno do PostgreSQL", written)
        )
    if anomalies:
        sections.append(("⚠️ Anomálie (neopravují se automaticky)", anomalies))
    if failed:
        sections.append(("❌ Chyby zpracování", failed))

    if preview:
        footer = (
            "Náhled – nic se nezapsalo. Observe-only – Discord role se NEMĚNÍ."
            + (
                " Zápis potvrď tlačítkem."
                if outcome.tier_changes
                else " Mirror už odpovídá Discordu."
            )
        )
        title = f"{status_icon} /sync discord – Discord → PostgreSQL mirror (náhled)"
    else:
        footer = (
            "Observe-only – Discord role se NEMĚNÍ. Discord je jediná "
            f"autorita aktuálních tierů. Run: #{outcome.sync_run_id}."
        )
        title = f"{status_icon} /sync discord – Discord → PostgreSQL mirror"
    embeds = build_embed_pack(
        title=title,
        description=description,
        color=(
            0x10B981
            if outcome.status == SYNC_RUN_SUCCESS
            else 0xF59E0B
            if outcome.status == SYNC_RUN_PARTIAL
            else 0xEF4444
        ),
        footer=footer,
        sections=sections,
    )
    _append_note(embeds[0], note)
    return embeds


def _discord_failure_embed(err: Exception, *, preview: bool) -> discord.Embed:
    return discord.Embed(
        title="❌ /sync discord – databáze selhala",
        description=(
            f"{'Náhled se nepodařil' if preview else 'Mirror se NEzapsal'} "
            f"(`{err}`). Discord role se vůbec neměnily – jsou i nadále "
            "jedinou autoritou. Zkuste to znovu; přetrvává-li problém, "
            "zkontrolujte PostgreSQL."
        ),
        color=0xEF4444,
    )


async def _discord_sync(
    interaction: discord.Interaction, session_factory, *, dry_run: bool
) -> DiscordSyncOutcome:
    members = await guild_members(interaction.guild)
    return await DiscordSyncService().sync_guild(
        session_factory,
        members=members,
        triggered_by=interaction.user.id,
        triggered_by_name=str(interaction.user),
        command="sync",
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Potvrzovací view (přesunuto beze změny logiky ze starých cogů)
# ---------------------------------------------------------------------------
class SyncDiscordConfirmView(SafeView):
    """Tlačítko „Zapsat do databáze" pro /sync discord."""

    def __init__(self):
        super().__init__(timeout=120)
        self.finished = False

    @discord.ui.button(
        label="✅ Zapsat do databáze",
        style=discord.ButtonStyle.success,
        custom_id="sync_discord_confirm",
    )
    async def confirm(self, interaction: discord.Interaction, button) -> None:
        if (msg := admin_gate_error(interaction)) is not None:
            return await interaction.response.send_message(msg, ephemeral=True)
        if self.finished:
            return await interaction.response.send_message(
                "✅ Synchronizace už proběhla.", ephemeral=True
            )
        self.finished = True
        await interaction.response.defer(ephemeral=True)

        session_factory = getattr(interaction.client, "db_session_factory", None)
        try:
            outcome = await _discord_sync(
                interaction, session_factory, dry_run=False
            )
        except Exception as err:  # noqa: BLE001 – selhání DB = tvrdá chyba
            log.exception("Selhání /sync discord (PostgreSQL)")
            self.finished = False
            await interaction.followup.send(
                embed=_discord_failure_embed(err, preview=False), ephemeral=True
            )
            return

        embeds = _discord_embeds(outcome, preview=False)
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit potvrzovací zprávu: %s", err)
        await _send_embed_pack(interaction.followup, embeds)


class SyncDiscordRollbackView(SafeView):
    """Tlačítko „Potvrdit a vrátit změny" pro /sync rollback apply.

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

        self.finished = True
        await interaction.response.defer(ephemeral=True)

        # 1) Ověření, že se audit od náhledu nezměnil → vrátíme PŘESNĚ to,
        #    co admin potvrdil (nikdy nic automaticky navíc).
        entries = await get_playersync_log()
        fresh_entry, _warnings = find_rollback_target(
            entries, target_ts=self.plan["target_ts"]
        )
        if fresh_entry is None:
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

        await self._finish(interaction, results=results, stale=False)

    async def _finish(self, interaction, *, results, stale) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /sync rollback – audit se změnil",
                description=(
                    "Mezitím se změnil cílový auditní záznam – **nic jsem "
                    "nevrátil**. Spusť **/sync rollback mode:apply** "
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
                title="↩️ /sync rollback – dokončeno",
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

        self.finished = True
        await interaction.response.defer(ephemeral=True)

        # Ověření, že se kanonická DB od náhledu nezměnila – nahrajeme PŘESNĚ
        # to, co admin potvrdil (nikdy nic automaticky navíc).
        session_factory = getattr(interaction.client, "db_session_factory", None)
        try:
            canonical = await _canonical_for_export(session_factory)
        except Exception:  # noqa: BLE001 – žádný fallback na players.json
            log.exception("Export z PostgreSQL selhal – nic se neposílá")
            await self._finish(
                interaction,
                result={
                    "ok": False,
                    "message": (
                        "PostgreSQL nedostupný – **nic jsem na web neposlal**. "
                        "Nahrát se nesmí nic jiného než data z DB; zkontroluj "
                        "spojení a spusť **/sync web mode:apply** znovu."
                    ),
                },
            )
            return
        if not canonical or fingerprint_canonical(canonical) != self.fingerprint:
            await self._finish(interaction, result=None, stale=True)
            return

        result = await sync_website(
            canonical=canonical,
            message=SYNC_MESSAGE,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
        )
        await self._finish(interaction, result=result, stale=False)

    async def _finish(self, interaction, *, result=None, stale=False) -> None:
        if stale:
            embed = discord.Embed(
                title="🔄 /sync web – stav se změnil",
                description=(
                    "Kanonická DB se mezitím změnila – **nic jsem "
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


# ---------------------------------------------------------------------------
# Cog – orchestrace (business logika zůstává ve službách)
# ---------------------------------------------------------------------------
class Sync(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    sync = app_commands.Group(
        name="sync",
        description="Centrální synchronizace: discord / web / check / rollback",
    )

    # ------------------------------------------------------------------
    # /sync check
    # ------------------------------------------------------------------
    @sync.command(
        name="check",
        description="Read-only diagnostika: Discord × PostgreSQL × web + health",
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

        session_factory = getattr(interaction.client, "db_session_factory", None)
        try:
            players = await _canonical_players(session_factory)
            roles_map = await _kit_role_maps_or_empty(session_factory)
            kit_display = await kit_display_map(session_factory=session_factory)
            members = [
                member_to_dict(m) for m in await guild_members(interaction.guild)
            ]
        except Exception:  # noqa: BLE001 – po defer musí admin dostat odpověď
            log.exception("/sync check – načtení dat selhalo")
            return await interaction.followup.send(
                "❌ **/sync check se nepodařil** – PostgreSQL nebo Discord "
                "nejsou dostupné. Nic se nezměnilo, zkus to znovu.",
                ephemeral=True,
            )

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
            note=note,
        )
        # G0: health report z `db.services.health` dostává v /sync check svého
        # prvního produkčního volajícího. Přidává se jako SAMOSTATNÝ embed
        # (ne jako položka `items`), takže filtrování podle oblasti i počty
        # severit zůstávají přesně tam, kde byly.
        if area in ("all", "db"):
            embeds = embeds + await _db_health_embed(session_factory)
        await _send_embed_pack(interaction.followup, embeds)

    # ------------------------------------------------------------------
    # /sync discord (observe-only: Discord → PostgreSQL mirror)
    # ------------------------------------------------------------------
    @sync.command(
        name="discord",
        description="Synchronizace Discord → PostgreSQL: náhled změn a zápis po potvrzení",
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

        try:
            outcome = await _discord_sync(interaction, session_factory, dry_run=True)
        except Exception as err:  # noqa: BLE001 – selhání DB = tvrdá chyba
            log.exception("Selhání /sync discord (PostgreSQL)")
            await interaction.followup.send(
                embed=_discord_failure_embed(err, preview=True), ephemeral=True
            )
            return

        embeds = _discord_embeds(outcome, preview=True, note=note)
        view = SyncDiscordConfirmView() if outcome.tier_changes else None
        await _send_embed_pack(interaction.followup, embeds, view=view)

    # ------------------------------------------------------------------
    # /sync rollback
    # ------------------------------------------------------------------
    @sync.command(
        name="rollback",
        description="Vrátí poslední aplikovaný sync rolí (dry run defaultně)",
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
    async def sync_rollback(
        self,
        interaction: discord.Interaction,
        mode: str = "preview",
        target_ts: int | None = None,
    ) -> None:
        await self._run_rollback(
            interaction, mode=mode, target_ts=target_ts
        )

    async def _run_rollback(
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
                title="❌ /sync rollback – nelze",
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
                title="❌ /sync rollback – chybí informace",
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
                title="ℹ️ /sync rollback – není co vracet",
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
                title="❌ /sync rollback – bezpečnostní kontrola selhala",
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
        try:
            canonical = await _canonical_for_export(session_factory)
        except Exception:  # noqa: BLE001 – žádný fallback na players.json
            log.exception("Export z PostgreSQL selhal – /sync web neproběhne")
            return await interaction.followup.send(
                "❌ **PostgreSQL nedostupný – export neproběhl.**\n"
                "Na web se nesmí nahrát nic jiného než data z DB (dřív se sem "
                "tichy dostal legacy `data/players.json` a vznikl rozestup, "
                "který nikdo neviděl). Zkontroluj spojení a opakuj.",
                ephemeral=True,
            )
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
            # preview = read-only náhled: view jen při mode:"apply" s rozdíly
            await _send_embed_pack(interaction.followup, embeds)
            return

        view = SyncWebConfirmView(
            canonical_fingerprint=result["fingerprint"]
        )
        await _send_embed_pack(interaction.followup, embeds, view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sync(bot))

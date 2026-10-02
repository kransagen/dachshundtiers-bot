"""Diagnostika Discord × PostgreSQL × web – podklad pro /sync check (čistá logika, bez discord.py).

Analýza NIKDY nic nezapisuje. Pro každého hráče a kit porovnává tři zdroje:

  - Discord tier role (kit_roles + členové serveru),
  - kanonická databáze (PostgreSQL mirror, export ``services.player_export``),
  - web / GitHub (``players.json`` v repozitáři DachshundTiers).

Detekované statusy:
  - ``MATCH``                – Discord role = DB = web
  - ``MISSING_DISCORD_ROLE`` – hráč má tier v DB/webu, ale roli nemá
  - ``DATABASE_MISMATCH``    – Discord role ≠ DB (mirror srovná ``/sync discord``)
  - ``WEBSITE_MISMATCH``     – web ≠ DB (Discord = DB; srovná ``/sync web``)
  - ``MULTIPLE_TIER_ROLES``  – hráč drží víc tier rolí stejného kitu = KONFLIKT,
                               nikdy se nevybírá automaticky
  - ``UNKNOWN_ROLE``         – role namapovaná na neregistrovaný kit
  - ``UNKNOWN_PLAYER``       – člen drží tier roli, ale v DB není
  - ``DUPLICATE_PLAYER``     – stejný hráč (username) víc krát v DB / na webu

Každý běh /sync check se zapisuje do ``data/checkweb_log.json`` (append-only,
restart-safe, posledních ``AUDIT_LOG_LIMIT`` záznamů).

Zdroj pravdy zůstává hodnocení (``/result``) a Discord role. Web se tímto
modulem nemění (slouží na to ``/sync web``).
"""

import logging
import time

from services.datacheck import canonical_tier
from services.store import read as store_read, transaction

log = logging.getLogger("dachshundtiers")

CHECKWEB_LOG_FILE = "checkweb_log.json"
AUDIT_LOG_LIMIT = 1000

# Statusy v pořadí pro souhrn / zobrazení
STATUSES = (
    "MATCH",
    "MISSING_DISCORD_ROLE",
    "DATABASE_MISMATCH",
    "WEBSITE_MISMATCH",
    "MULTIPLE_TIER_ROLES",
    "UNKNOWN_ROLE",
    "UNKNOWN_PLAYER",
    "DUPLICATE_PLAYER",
)

STATUS_LABELS = {
    "MATCH": "✅ Shoda",
    "MISSING_DISCORD_ROLE": "➕ Chybějící Discord role",
    "DATABASE_MISMATCH": "✏️ Databáze se neshoduje",
    "WEBSITE_MISMATCH": "🌐 Web se neshoduje",
    "MULTIPLE_TIER_ROLES": "🔁 Víc tier rolí (KONFLIKT)",
    "UNKNOWN_ROLE": "❓ Neznámá role",
    "UNKNOWN_PLAYER": "👤 Neznámý hráč",
    "DUPLICATE_PLAYER": "👥 Duplicitní hráč",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _username(p) -> str:
    return str((p or {}).get("username", "") or "").strip()


def _mode_value(modes, kit_key):
    """Hodnota módu pro kit (case-insensitive) – (klíč, tier), jinak (None, None)."""
    kit_key = str(kit_key).strip().lower()
    for key, value in (modes or {}).items():
        if str(key).strip().lower() == kit_key:
            return key, value
    return None, None


def _tier_clean(value):
    """Porovnatelná + čitelná podoba tieru (canonical, jinak upperspace)."""
    if value is None:
        return None
    canon = canonical_tier(value)
    if canon is not None:
        return canon
    s = str(value).strip().upper()
    return s or None


def _render(player, kit, db, web, discord, status) -> str:
    """Zobrazení záznamu ve formátu DATABASE / DISCORD / WEBSITE."""
    discs = ", ".join(sorted(t for t in (discord or []) if t)) or "—"
    lines = [
        f"**{player}** · **{kit}**",
        f"DATABASE: {db or '—'}",
        f"DISCORD: {discs}",
        f"WEBSITE: {web or '—'}",
    ]
    return "\n".join(lines)


def _record(
    *,
    player,
    kit_key,
    kit,
    db,
    web,
    discord,
    status,
    scope="player",
    member_id=None,
    member_name="",
    message=None,
):
    """Jeden porovnávací záznam (hráč × kit)."""
    return {
        "player": player,
        "kit": kit,
        "kit_key": str(kit_key).strip().lower(),
        "db": db,
        "web": web,
        "discord": sorted({t for t in (discord or []) if t}),
        "status": status,
        "label": (
            "⚠️ KONFLIKT"
            if status == "MULTIPLE_TIER_ROLES"
            else STATUS_LABELS.get(status, status)
        ),
        "scope": scope,
        "member_id": str(member_id) if member_id not in (None, "") else None,
        "member_name": member_name or "",
        "message": message or _render(player, kit, db, web, discord, status),
    }


# ---------------------------------------------------------------------------
# Analýza (čistá funkce – nic neaplikuje)
# ---------------------------------------------------------------------------
def analyze_checkweb(*, players, website=None, members=None, roles_map=None, kit_display=None) -> dict:
    """Porovná Discord role × PostgreSQL mirror × web; vrátí záznamy + souhrn.

    ``players`` je canonical data z DB (v PostgreSQL režimu
    ``services.player_export.export_players``), nikoli players.json – sloupec
    ``db`` v záznamech je tedy skutečné zrcadlo v ``player_current_tiers``.

    ``website=None`` = web se nepodařilo přečíst (bez GITHUB_TOKEN / chyba) –
    porovnání s webem se přeskočí (status WEBSITE_MISMATCH nikdy nevznikne).

    Vrací::
        {
          "records":     [záznam (hráč × kit), ...]  # deterministicky seřazené
          "summary":     {status: počet},             # všechny kategorie, i 0
          "checked":     počet záznamů,
          "has_issues":  True, když existuje záznam ≠ MATCH,
        }
    """
    kit_display = {str(k).strip().lower(): v for k, v in (kit_display or {}).items()}
    players = [p for p in (players or []) if isinstance(p, dict)]
    website = list(website) if isinstance(website, list) else website
    members = [m for m in (members or []) if isinstance(m, dict) and str(m.get("id", ""))]
    roles_map = roles_map or {}

    # ---- indexy -----------------------------------------------------------
    db_index: dict[str, list] = {}
    for p in players:
        key = _username(p).lower()
        if key:
            db_index.setdefault(key, []).append(p)

    web_index: dict[str, list] = {}
    for w in (website or []):
        if isinstance(w, dict):
            key = _username(w).lower()
            if key:
                web_index.setdefault(key, []).append(w)

    registered = set(kit_display)

    # role id -> [(kit_key, tier)]; role namapovaná na neregistrovaný kit zvlášť
    role_to_kit: dict[str, list] = {}
    role_unknown_kit: dict[str, list] = {}
    for kit_key, kit_map in roles_map.items():
        kit_key = str(kit_key).strip().lower()
        if not kit_key or not isinstance(kit_map, dict):
            continue
        for tier, role_id in kit_map.items():
            tier_up = str(tier).strip().upper()
            rid = str(role_id).strip()
            if not tier_up or not rid.isdigit():
                continue
            table = role_to_kit if kit_key in registered else role_unknown_kit
            table.setdefault(rid, []).append((kit_key, tier_up))

    records: list = []
    summary = {s: 0 for s in STATUSES}

    def _emit(
        *,
        player,
        kit_key,
        kit,
        db,
        web,
        discord=(),
        status,
        scope="player",
        member_id=None,
        member_name="",
        message=None,
    ) -> None:
        summary[status] += 1
        records.append(
            _record(
                player=player,
                kit_key=kit_key,
                kit=kit,
                db=db,
                web=web,
                discord=discord,
                status=status,
                scope=scope,
                member_id=member_id,
                member_name=member_name,
                message=message,
            )
        )

    def _discord_tiers(member_list, kit_key) -> set:
        out = set()
        for m in member_list:
            for rid in m.get("role_ids") or []:
                for kk, tier in role_to_kit.get(str(rid), []):
                    if kk == kit_key:
                        clean = _tier_clean(tier)
                        if clean:
                            out.add(clean)
        return out

    def _db_web_tier(player, web_players, kit_key):
        db_raw = _mode_value(player.get("modes") or {}, kit_key)[1]
        db = _tier_clean(db_raw)
        web = None
        for w in web_players:
            w_raw = _mode_value(w.get("modes") or {}, kit_key)[1]
            cand = _tier_clean(w_raw)
            if cand:
                web = cand
                break
        return db, web

    # ---- duplicitní hráči --------------------------------------------------
    for key, group in sorted(db_index.items()):
        if len(group) > 1:
            _emit(
                player=_username(group[0]),
                kit_key="",
                kit="(hráč)",
                db=None,
                web=None,
                status="DUPLICATE_PLAYER",
                scope="database",
                message=(
                    f"👥 **{_username(group[0])}** je v players.json **{len(group)}×** "
                    "– sloučit ručně, před řešením ostatních nálezů."
                ),
            )
    for key, group in sorted(web_index.items()):
        if len(group) > 1 and len(db_index.get(key, [])) <= 1:
            _emit(
                player=_username(group[0]),
                kit_key="",
                kit="(hráč)",
                db=None,
                web=None,
                status="DUPLICATE_PLAYER",
                scope="website",
                message=(
                    f"👥 **{_username(group[0])}** je na webu **{len(group)}×** "
                    "– sjednotí se při `/sync web`."
                ),
            )

    skip_players = {key for key, group in db_index.items() if len(group) > 1}

    # ---- párování člen ↔ hráč ----------------------------------------------
    matched: dict[str, list] = {}
    unknown_members: list = []
    for m in members:
        found = None
        for n in m.get("names") or []:
            nkey = str(n).strip().lower()
            if nkey in db_index:
                found = nkey
                break
        if found is None:
            unknown_members.append(m)
        else:
            matched.setdefault(found, []).append(m)

    def _member_names(member_list) -> str:
        return ", ".join(str(m.get("name", "") or "") for m in member_list)

    # ---- hráč × kit (kanonická DB) ------------------------------------------
    for key in sorted(k for k in db_index if k not in skip_players):
        p = db_index[key][0]
        web_players = web_index.get(key, [])
        mlist = matched.get(key, [])
        username = _username(p)

        kits = set()
        for mk in (p.get("modes") or {}):
            mkey = str(mk).strip().lower()
            if mkey:
                kits.add(mkey)
        for w in web_players:
            for mk in (w.get("modes") or {}):
                mkey = str(mk).strip().lower()
                if mkey:
                    kits.add(mkey)
        for m in mlist:
            for rid in m.get("role_ids") or []:
                for kk, _tier in role_to_kit.get(str(rid), []):
                    kits.add(kk)

        for kit_key in sorted(kits):
            kit_name = kit_display.get(kit_key) or kit_key.capitalize()
            db, web = _db_web_tier(p, web_players, kit_key)
            discord_tiers = _discord_tiers(mlist, kit_key)

            if not discord_tiers:
                if db is None and web is None:
                    continue
                _emit(
                    player=username,
                    kit_key=kit_key,
                    kit=kit_name,
                    db=db,
                    web=web,
                    discord=(),
                    status="MISSING_DISCORD_ROLE",
                    member_name=_member_names(mlist) or None,
                    message=(
                        _render(username, kit_name, db, web, (), "MISSING_DISCORD_ROLE")
                        + "\n💡 Role chybí – doplň ji přes `/result` (Discord role se z DB nerozdávají hromadně)."
                    ),
                )
                continue

            if len(discord_tiers) > 1:
                _emit(
                    player=username,
                    kit_key=kit_key,
                    kit=kit_name,
                    db=db,
                    web=web,
                    discord=discord_tiers,
                    status="MULTIPLE_TIER_ROLES",
                    member_name=_member_names(mlist) or None,
                    message=(
                        _render(username, kit_name, db, web, discord_tiers, "MULTIPLE_TIER_ROLES")
                        + "\n⚠️ Hráč drží víc tier rolí najednou – žádná se NEvybírá automaticky, nech jen jednu."
                    ),
                )
                continue

            d = next(iter(discord_tiers))
            if db == d:
                if web is not None and web != d:
                    _emit(
                        player=username,
                        kit_key=kit_key,
                        kit=kit_name,
                        db=db,
                        web=web,
                        discord=discord_tiers,
                        status="WEBSITE_MISMATCH",
                        member_name=_member_names(mlist) or None,
                        message=(
                            _render(username, kit_name, db, web, discord_tiers, "WEBSITE_MISMATCH")
                            + "\n🌐 DB a Discord souhlasí, web je zastaralý – srovná ho `/sync web`."
                        ),
                    )
                else:
                    _emit(
                        player=username,
                        kit_key=kit_key,
                        kit=kit_name,
                        db=db,
                        web=web,
                        discord=discord_tiers,
                        status="MATCH",
                        member_name=_member_names(mlist) or None,
                    )
            else:
                _emit(
                    player=username,
                    kit_key=kit_key,
                    kit=kit_name,
                    db=db,
                    web=web,
                    discord=discord_tiers,
                    status="DATABASE_MISMATCH",
                    member_name=_member_names(mlist) or None,
                    message=(
                        _render(username, kit_name, db, web, discord_tiers, "DATABASE_MISMATCH")
                        + "\n✏️ Discord role se liší od DB – mirror srovná `/sync discord`."
                    ),
                )

    # ---- členové bez záznamu v DB (UNKNOWN_PLAYER / UNKNOWN_ROLE) ----------
    for m in unknown_members:
        alias = str(m.get("name", "") or "") or str(m["id"])
        held: dict[str, set] = {}
        for rid in m.get("role_ids") or []:
            for kk, tier in role_to_kit.get(str(rid), []):
                clean = _tier_clean(tier)
                if clean:
                    held.setdefault(kk, set()).add(clean)
            for kk, tier in role_unknown_kit.get(str(rid), []):
                _emit(
                    player=alias,
                    kit_key=kk,
                    kit=f"{kk} (unregistered)",
                    db=None,
                    web=None,
                    discord=[_tier_clean(tier) or tier],
                    status="UNKNOWN_ROLE",
                    scope="role",
                    member_id=str(m["id"]),
                    member_name=alias,
                    message=(
                        f"❓ **{alias}** drží roli namapovanou na **neregistrovaný kit** "
                        f"`{kk}` – zkontroluj `/setkitrole` ručně."
                    ),
                )
        for kk in sorted(held):
            kit_name = kit_display.get(kk) or kk.capitalize()
            web_players = [
                w
                for n in m.get("names") or []
                for w in web_index.get(str(n).strip().lower(), [])
            ]
            web = None
            for w in web_players:
                w_raw = _mode_value(w.get("modes") or {}, kk)[1]
                cand = _tier_clean(w_raw)
                if cand:
                    web = cand
                    break
            tiers = sorted(held[kk])
            note = (
                f" (víc rolí: {', '.join(tiers)})"
                if len(tiers) > 1
                else ""
            )
            _emit(
                player=alias,
                kit_key=kk,
                kit=kit_name,
                db=None,
                web=web,
                discord=tiers,
                status="UNKNOWN_PLAYER",
                scope="member",
                member_id=str(m["id"]),
                member_name=alias,
                message=(
                    _render(alias, kit_name, None, web, tiers, "UNKNOWN_PLAYER")
                    + f"\n👤 V databázi žádný takový hráč není{note} – "
                    "doplň ho přes `/result` (zdroj pravdy), role je projekce DB."
                ),
            )

    # deterministické pořadí: status, pak abeceda
    order = {s: i for i, s in enumerate(STATUSES)}
    records.sort(
        key=lambda r: (
            order.get(r.get("status"), 99),
            str(r.get("player", "")).lower(),
            str(r.get("kit_key", "")),
        )
    )

    return {
        "records": records,
        "summary": summary,
        "checked": len(records),
        "has_issues": any(r.get("status") != "MATCH" for r in records),
    }


# ---------------------------------------------------------------------------
# Auditní log (data/checkweb_log.json, append-only, restart-safe)
# ---------------------------------------------------------------------------
async def log_checkweb_event(
    *,
    actor_id=None,
    actor_name="",
    mode: str,
    status: str = "success",
    summary=None,
    website=None,
    errors=None,
    ts: int = None,
) -> dict:
    """Přidá záznam o běhu /sync check do auditu."""
    if ts is None:
        ts = _now_ms()
    entry: dict = {
        "ts": ts,
        "mode": mode,          # "preview"
        "status": status,
        "summary": dict(summary or {}),
        "website": website,    # "GitHub" | "nedostupný..." | None
        "errors": list(errors or []),
    }
    if actor_id is not None:
        entry["actorId"] = str(actor_id)
        entry["actorName"] = actor_name or ""

    async def _run(tx):
        entries = tx.get(CHECKWEB_LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        tx.set(CHECKWEB_LOG_FILE, entries[-AUDIT_LOG_LIMIT:])
        return entry

    return await transaction((CHECKWEB_LOG_FILE,), _run)


async def get_checkweb_log() -> list:
    """Všechny záznamy auditu v pořadí zápisu (chronologicky)."""
    entries = await store_read(CHECKWEB_LOG_FILE, [])
    return [e for e in entries if isinstance(e, dict)]

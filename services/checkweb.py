"""Bezpečná synchronizace webu – /checkweb (čistá logika, bez discord.py).

Nová verze /checkweb NIKDY nic nezapisuje automaticky (původní verze brala
Discord tier role jako autoritativní zdroj a sama přepisovala players.json
i historii – toto chování je zrušené). Pro každého hráče a kit porovnává tři
zdroje:

  - Discord tier role (``data/kit_roles.json`` + členové serveru),
  - kanonická databáze (``data/players.json``),
  - web / GitHub (``players.json`` v repozitáři DachshundTiers).

Detekované statusy (názvy podle požadavku):
  - ``MATCH``                – Discord role = DB = web
  - ``MISSING_DISCORD_ROLE`` – hráč má tier v DB/webu, ale roli nemá
  - ``DATABASE_MISMATCH``    – Discord role ≠ DB (DB se smí opravit jen
                               po explicitním potvrzení)
  - ``WEBSITE_MISMATCH``     – web ≠ DB (Discord = DB; opraví ``/websync``)
  - ``MULTIPLE_TIER_ROLES``  – hráč drží víc tier rolí stejného kitu = KONFLIKT,
                               nikdy se nevybírá automaticky
  - ``UNKNOWN_ROLE``         – role namapovaná v kit_roles.json na
                               neregistrovaný kit
  - ``UNKNOWN_PLAYER``       – člen drží tier roli, ale v players.json není
  - ``DUPLICATE_PLAYER``     – stejný hráč (username) víc krát v DB / na webu

Opravit databázi lze JEN v ``/checkweb apply`` a JEN po explicitním
per-záznamovém rozhodnutí (akce ``use_discord`` / ``keep_database`` /
``ignore``):
  - Use Discord    → tier v DB := Discord tier (BEZ záznamu do historie!),
  - Keep Database  → ponechat tier v DB,
  - Ignore         → nechat být.

Každé rozhodnutí (i „keep" / „ignore") se zapisuje do ``data/checkweb_log.json``
(append-only, restart-safe) s: actor, player, kit, old tier, new tier, reason,
timestamp a source.

Zdroj pravdy zůstává hodnocení (``/result``). Discord role jsou projekce DB,
ne naopak. Web se tímto příkazem nemění (slouží na to ``/websync``).
"""

import logging
import time

from services.datacheck import canonical_tier
from services.store import read as store_read, transaction

log = logging.getLogger("dachshundtiers")

CHECKWEB_LOG_FILE = "checkweb_log.json"

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

# Statusy, které může admin vyřešit v /checkweb apply
RESOLVABLE_STATUSES = ("DATABASE_MISMATCH", "MULTIPLE_TIER_ROLES")


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


def _decision_options(db, discord) -> list:
    """Možná rozhodnutí pro záznam ([Use Discord] / [Keep Database] / [Ignore]).

    U KONFLIKTU (víc rolí) se nabídne jeden „Use Discord" na každou roli –
    nikdy se nevybírá automaticky.
    """
    options = []
    for t in sorted({t for t in (discord or []) if t}):
        options.append({"key": f"use_discord:{t}", "label": f"✅ Use Discord ({t})"})
    if db:
        options.append({"key": "keep_database", "label": f"🏛️ Keep Database ({db})"})
    options.append({"key": "ignore", "label": "🚫 Ignore"})
    return options


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
        "resolvable": status in RESOLVABLE_STATUSES,
        "options": _decision_options(db, discord) if status in RESOLVABLE_STATUSES else [],
    }


# ---------------------------------------------------------------------------
# Analýza (čistá funkce – nic neaplikuje)
# ---------------------------------------------------------------------------
def analyze_checkweb(*, players, website=None, members=None, roles_map=None, kit_display=None) -> dict:
    """Porovná Discord role × players.json × web; vrátí záznamy + souhrn.

    ``website=None`` = web se nepodařilo přečíst (bez GITHUB_TOKEN / chyba) –
    porovnání s webem se přeskočí (status WEBSITE_MISMATCH nikdy nevznikne).

    Vrací::
        {
          "records":     [záznam (hráč × kit), ...]  # deterministicky seřazené
          "summary":     {status: počet},             # všechny kategorie, i 0
          "checked":     počet záznamů,
          "has_issues":  True, když existuje záznam ≠ MATCH,
          "resolvable":  [záznamy řešitelné v /checkweb apply],
          "fingerprint": otisk řešitelných záznamů (preview vs potvrzení),
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
                    "– sjednotí se při `/websync apply`."
                ),
            )

    skip_players = {key for key, group in db_index.items() if len(group) > 1}

    # ---- párování člen ↔ hráč ----------------------------------------------
    members_by_name: dict[str, dict] = {}
    for m in members:
        for n in m.get("names") or []:
            nkey = str(n).strip().lower()
            if nkey and nkey not in members_by_name:
                members_by_name[nkey] = m

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
                        + "\n💡 Role chybí – doplní ji `/playersync apply` (Discord role je projekce DB)."
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
                        + "\n⚠️ Hráč drží víc tier rolí najednou – žádná se NEvybírá automaticky, rozhodni v `/checkweb apply`."
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
                            + "\n🌐 DB a Discord souhlasí, web je zastaralý – srovná ho `/websync apply`."
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
                        + "\n✏️ Discord role se liší od DB – opravu povolíš jen svým rozhodnutím."
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
                        f"`{kk}` – zkontroluj `/setkitrole`/`kits.json` ručně."
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
                    + f"\n👤 V players.json žádný takový hráč není{note} – "
                    "doplň ho přes `/result` (zdroj pravdy), role je projekce DB."
                ),
            )

    # deterministické pořadí: k řešení první, pak status, pak abeceda
    order = {s: i for i, s in enumerate(STATUSES)}
    records.sort(
        key=lambda r: (
            0 if r.get("resolvable") else 1,
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
        "resolvable": [r for r in records if r.get("resolvable")],
        "fingerprint": fingerprint_resolvable(records),
    }


def fingerprint_resolvable(records) -> str:
    """Deterministický otisk řešitelných záznamů – porovnání preview vs potvrzení.

    Změní-li se mezi náhledem a potvrzením role, DB nebo web u některého
    záznamu, otisk se liší a nic se neaplikuje.
    """
    lines = []
    for r in (records or []):
        if not r.get("resolvable"):
            continue
        lines.append(
            "|".join(
                [
                    str(r.get("player", "")).lower(),
                    str(r.get("kit_key", "")).lower(),
                    str(r.get("db") or ""),
                    ",".join(r.get("discord") or []),
                    str(r.get("web") or ""),
                    str(r.get("status", "")),
                ]
            )
        )
    return "|".join(sorted(lines))


# ---------------------------------------------------------------------------
# Aplikace rozhodnutí (pouze po explicitním potvrzení)
# ---------------------------------------------------------------------------
def apply_checkweb_decisions(*, players, records, decisions, kit_display=None) -> tuple:
    """Aplikuje per-záznamová rozhodnutí na kanonickou players.json.

    - ``use_discord``   → tier v DB := Discord tier (BEZ zápisu do historie!),
    - ``keep_database`` / ``ignore`` → DB se nemění (rozhodnutí se jen loguje).

    Vrací ``(new_players, applied)``; každý prvek ``applied`` má
    ``player, kit, kit_key, oldTier, newTier, reason, source, ok, error``.
    """
    kit_display = kit_display or {}
    index = {}
    for r in (records or []):
        if r.get("resolvable"):
            index[
                (str(r.get("player", "")).strip().lower(), str(r.get("kit_key", "")).strip().lower())
            ] = r

    new_players = [dict(p) for p in (players or []) if isinstance(p, dict)]
    by_name: dict[str, list] = {}
    for p in new_players:
        uname = str(p.get("username", "") or "").strip().lower()
        if uname:
            by_name.setdefault(uname, []).append(p)

    applied = []
    for dec in (decisions or []):
        player = str(dec.get("player", "") or "").strip()
        kit_key = str(dec.get("kit_key", "") or "").strip().lower()
        rec = index.get((player.lower(), kit_key))
        reason = str(dec.get("decision") or "ignore")
        old_tier = rec.get("db") if rec else None
        entry = {
            "player": player,
            "kit": rec.get("kit", "") if rec else kit_key,
            "kit_key": kit_key,
            "oldTier": old_tier,
            "newTier": old_tier,
            "reason": reason,
            "source": "discord" if reason == "use_discord" else "database",
            "ok": False,
            "error": None,
        }
        if rec is None:
            entry["error"] = "záznam už neexistuje"
            applied.append(entry)
            continue

        if reason == "use_discord":
            tier = _tier_clean(dec.get("tier"))
            if tier is None or tier not in (rec.get("discord") or []):
                entry["error"] = "neplatný Discord tier"
                applied.append(entry)
                continue
            targets = by_name.get(player.lower())
            if not targets:
                entry["error"] = "hráč v players.json není"
                applied.append(entry)
                continue
            p = targets[0]
            modes = p.setdefault("modes", {})
            if not isinstance(modes, dict):
                modes = {}
                p["modes"] = modes
            mode_key, _existing = _mode_value(modes, kit_key)
            if mode_key is None:
                mode_key = kit_display.get(kit_key) or kit_key.capitalize()
            # oldTier se bere z DB před zápisem (rec.db = stav před změnou)
            modes[mode_key] = tier
            entry["newTier"] = tier
            entry["source"] = "discord"
            entry["ok"] = True
        elif reason in ("keep_database", "ignore"):
            entry["newTier"] = old_tier
            entry["ok"] = True
        else:
            entry["error"] = f"neznámé rozhodnutí {reason}"
        applied.append(entry)
    return new_players, applied


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
    repairs=None,
    ts: int = None,
) -> dict:
    """Přidá záznam o použití /checkweb do auditu.

    Každé opravené rozhodnutí (repair) dostane kompletní auditní stopu:
    actor, player, kit, old tier, new tier, reason, timestamp a source.
    """
    if ts is None:
        ts = _now_ms()
    entry: dict = {
        "ts": ts,
        "mode": mode,          # "preview" | "apply"
        "status": status,
        "summary": dict(summary or {}),
        "website": website,    # "GitHub" | "nedostupný..." | None
        "errors": list(errors or []),
    }
    if actor_id is not None:
        entry["actorId"] = str(actor_id)
        entry["actorName"] = actor_name or ""

    repairs = list(repairs or [])
    if repairs:
        stamped = []
        for r in repairs:
            r = dict(r)
            r.setdefault("actor", actor_name or "")
            r.setdefault("actorId", str(actor_id) if actor_id is not None else None)
            r.setdefault("timestamp", ts)
            r.setdefault("source", r.get("source") or "database")
            stamped.append(r)
        entry["repairs"] = stamped

    async def _run(tx):
        entries = tx.get(CHECKWEB_LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        tx.set(CHECKWEB_LOG_FILE, entries)
        return entry

    return await transaction((CHECKWEB_LOG_FILE,), _run)


async def get_checkweb_log() -> list:
    """Všechny záznamy auditu v pořadí zápisu (chronologicky)."""
    entries = await store_read(CHECKWEB_LOG_FILE, [])
    return [e for e in entries if isinstance(e, dict)]
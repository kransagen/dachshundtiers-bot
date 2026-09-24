"""Kontrola integrity dat – /datacheck (čistá logika, bez discord.py).

Projede VŠECHNY místní databáze (kanonická players.json je zdroj pravdy,
nic se nekopíruje do druhé databáze) a hlásí problémy:

  - duplicate_players           – players.json: stejný username vícekrát
  - duplicate_discord_ids       – jeden ownerId s víc IGN / duplicitní ID v testers.json
  - duplicate_ign               – jedno IGN vlastněné víc Discord účty (tickety)
  - invalid_tiers               – tier mimo známou hierarchii (ladder/R/turnajové S-A)
  - conflicting_discord_roles   – jedna role namapovaná na víc (kit, tier)
  - missing_website_records     – modes má kit, ale chybí historie (web by byl neúplný)
  - invalid_eval_references     – evals.json ukazuje na neznámý kit / neexistujícího hráče
  - duplicate_kit_names         – kits.json: stejný kit s jiným case („MolePVP" i „molepvp")
  - duplicate_modes_keys        – players.json: jeden kit zapsaný 2× v modes pod jiným case
  - orphaned_tickets            – ticket, jehož kanál už neexistuje / poškozený záznam
  - orphaned_results            – výsledek bez hráče v players.json / bez ticketu

NIC se automaticky nemaže. Bezpečné opravy (nevratné mazání neprovádí).
Všechno ostatní se jen hlásí s návrhem. Aplikaci oprav vyžaduje explicitní
potvrzení (tlačítko v cogs). Audit: ``data/datacheck_log.json`` (append-only,
restart-safe) – každá kontrola i provedená oprava se zapisuje.
"""

import logging
import time

from services.store import read as store_read, transaction
from services.tickets import HT_TICKETS_FILE, HT3_TIER_LADDER, STATUS_CLOSED
from services.playersync import is_retired_tier

log = logging.getLogger("dachshundtiers")

DATACHECK_LOG_FILE = "datacheck_log.json"

KINDS = (
    "duplicate_players",
    "duplicate_discord_ids",
    "duplicate_ign",
    "invalid_tiers",
    "conflicting_discord_roles",
    "missing_website_records",
    "invalid_eval_references",
    "orphaned_tickets",
    "orphaned_results",
    "retired_tiers_in_modes",
    "duplicate_player_discord_ids",
    "duplicate_kit_names",
    "duplicate_modes_keys",
)

KIND_LABELS = {
    "duplicate_players": "👥 Duplicitní hráči",
    "duplicate_discord_ids": "🆔 Duplicitní Discord ID",
    "duplicate_ign": "🔤 Duplicitní IGN",
    "invalid_tiers": "🚫 Neplatné tiery",
    "conflicting_discord_roles": "🎭 Konfliktní Discord role",
    "missing_website_records": "🌐 Chybějící webové záznamy",
    "invalid_eval_references": "🎓 Neplatné eval reference",
    "orphaned_tickets": "🎟️ Osamocené tickety",
    "orphaned_results": "📋 Osamocené výsledky",
    "retired_tiers_in_modes": "🧓 Retired tiery v modes",
    "duplicate_player_discord_ids": "🆔 Duplicitní discordId hráčů",
    "duplicate_kit_names": "🔁 Duplicitní názvy kitů",
    "duplicate_modes_keys": "🔁 Duplicitní klíče kitů v modes",
}

# Známý vesmír tierů: žebříček + aliasy LT3-evalu + turnajové S/A/B + R-tiery.
KNOWN_TIERS = (
    set(HT3_TIER_LADDER)
    | {"LT3 EVAL", "LT3 EVALUATION", "LT3+EVAL"}
    | {"S", "A", "B", "C", "D", "E"}
    | {f"R{t}" for t in HT3_TIER_LADDER if t != "LT3E"}
)

TIER_ALIASES = {
    "LT3 EVAL": "LT3E",
    "LT3 EVALUATION": "LT3E",
    "LT3+EVAL": "LT3E",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _username(p: dict) -> str:
    return str(p.get("username", "") or "").strip()


def _normalize_key(value) -> str:
    return str(value or "").strip().lower()


def canonical_tier(value):
    """Kanonická podoba tieru (normalizace), nebo None pro neznámý/neplatný.

    Opravitelné jsou jen hodnoty, které jdou bezztrátově převézt (upper/trim,
    „LT3 EVAL" → LT3E). Všechno ostatní zůstává „invalid_tiers" bez opravy.
    """
    if value is None:
        return None
    s = str(value).strip().upper()
    s = TIER_ALIASES.get(s, s)
    if s not in KNOWN_TIERS:
        return None
    return s


def _finding(kind, severity, message, repair=None):
    return {
        "kind": kind,
        "severity": severity,
        "message": message,
        "repair": repair,
    }


def _tier_repair(username, kit, value, field, index=None):
    """Bezpečná oprava tieru (normalizace) nebo None."""
    canon = canonical_tier(value)
    if canon is None:
        return None
    raw = str(value)
    if canon == raw.strip():
        return None
    return {
        "action": "normalize_tier",
        "label": f"Normalizovat tier {raw} → {canon}",
        "targets": [
            {
                "username": username,
                "kit": kit,
                "field": field,   # "modes" | "history"
                "index": index,   # None pro modes, jinak pozice v historii
                "from": raw,
                "to": canon,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Jednotlivé kontroly (čisté funkce)
# ---------------------------------------------------------------------------
def check_duplicate_players(players: list) -> list:
    seen = {}
    for idx, p in enumerate(players or []):
        if not isinstance(p, dict):
            continue
        key = _normalize_key(_username(p))
        if not key:
            continue
        seen.setdefault(key, []).append(idx)
    out = []
    for key, indexes in sorted(seen.items()):
        if len(indexes) > 1:
            out.append(
                _finding(
                    "duplicate_players",
                    "error",
                    f"👥 **{key}** je v players.json {len(indexes)}× "
                    f"(záznamy #{[i + 1 for i in indexes]}). Sloučit ručně "
                    "(návrh: jedna kopie, spočítat historii).",
                )
            )
    return out


def check_ticket_identities(tickets: dict) -> list:
    """Duplicitní Discord ID (jeden owner, víc IGN) a duplicitní IGN (víc ownerů)."""
    owner_igns: dict[str, set] = {}
    ign_owners: dict[str, set] = {}
    for ticket in (tickets or {}).values():
        if not isinstance(ticket, dict):
            continue
        owner = str(ticket.get("ownerId", "") or "").strip()
        ign = _normalize_key(ticket.get("ign"))
        if not owner or not ign:
            continue
        owner_igns.setdefault(owner, set()).add(ign)
        ign_owners.setdefault(ign, set()).add(owner)

    out = []
    for owner, igns in sorted(owner_igns.items()):
        if len(igns) > 1:
            out.append(
                _finding(
                    "duplicate_discord_ids",
                    "error",
                    f"🆔 Discord účet **{owner}** vystupuje pod {len(igns)} IGN: "
                    f"{', '.join(sorted(igns))} – nejspíš špatný ticket.",
                )
            )
    for ign, owners in sorted(ign_owners.items()):
        if len(owners) > 1:
            out.append(
                _finding(
                    "duplicate_ign",
                    "error",
                    f"🔤 IGN **{ign}** vlastní {len(owners)} Discord účty: "
                    f"{', '.join(sorted(owners))} – konflikt identity.",
                )
            )
    return out


def check_duplicate_testers(testers: list) -> list:
    seen = {}
    for tid in (testers or []):
        key = str(tid or "").strip()
        if not key:
            continue
        seen.setdefault(key, []).append(tid)
    out = []
    for key, _ in sorted(seen.items()):
        if len(seen[key]) > 1:
            out.append(
                _finding(
                    "duplicate_discord_ids",
                    "error",
                    f"🆔 Tester ID **{key}** je v testers.json {len(seen[key])}×.",
                )
            )
    return out


def check_invalid_tiers(players: list) -> list:
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        username = _username(p)
        modes = p.get("modes") if isinstance(p.get("modes"), dict) else {}
        for kit, tier in sorted(modes.items()):
            finding = _tier_finding(
                username, str(kit), tier, "modes", index=None
            )
            if finding:
                out.append(finding)
        history = p.get("history") if isinstance(p.get("history"), dict) else {}
        for kit, entries in sorted(history.items()):
            if not isinstance(entries, list):
                continue
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                finding = _tier_finding(
                    username, str(kit), entry.get("tier"), "history", index=idx
                )
                if finding:
                    out.append(finding)
    return out


def _tier_finding(username, kit, tier, field, index=None):
    """Nález pro tier: neznámá hodnota (bez opravy) nebo nekanonický zápis
    (s bezpečnou opravou normalizace)."""
    canon = canonical_tier(tier)
    if canon is None:
        return _finding(
            "invalid_tiers",
            "warning",
            f"🚫 **{username}** · **{kit}** má neplatný tier "
            f"`{tier!r}` (mimo známou hierarchii).",
        )
    raw = str(tier).strip()
    if canon == raw:
        return None  # kanonický zápis – v pořádku
    return _finding(
        "invalid_tiers",
        "warning",
        f"🚫 **{username}** · **{kit}** má nekanonický zápis tieru "
        f"`{raw}` (kanonicky `{canon}`).",
        repair=_tier_repair(username, kit, tier, field, index=index),
    )


def check_kit_roles_conflicts(kit_roles: dict) -> list:
    role_map: dict[str, list] = {}
    for kit_key, tiers in (kit_roles or {}).items():
        if not isinstance(tiers, dict):
            continue
        for tier, role_id in tiers.items():
            rid = str(role_id or "").strip()
            if not rid.isdigit():
                continue  # nečíselný role_id → ignorovat (viz playersync)
            role_map.setdefault(rid, []).append((str(kit_key), str(tier).strip().upper()))
    out = []
    for rid, pairs in sorted(role_map.items()):
        if len(pairs) > 1:
            out.append(
                _finding(
                    "conflicting_discord_roles",
                    "error",
                    f"🎭 Role **{rid}** je namapovaná na {len(pairs)} (kit, tier): "
                    f"{', '.join(f'{k}/{t}' for k, t in sorted(pairs))} – "
                    "hráč s touto rolí by měl konfliktní tiery.",
                )
            )
    return out


def check_missing_website_records(players: list) -> list:
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        username = _username(p)
        modes = p.get("modes") if isinstance(p.get("modes"), dict) else {}
        history = p.get("history") if isinstance(p.get("history"), dict) else {}
        for kit in sorted(modes):
            entries = history.get(kit) if isinstance(history.get(kit), list) else []
            if not entries:
                out.append(
                    _finding(
                        "missing_website_records",
                        "warning",
                        f"🌐 **{username}** má tier pro **{kit}** v modes, ale "
                        "žádný záznam v historii – web by hráče zobrazil bez "
                        "historie. Doplň záznam (návrh: /result).",
                    )
                )
    return out


def check_retired_tiers_in_modes(players: list) -> list:
    """Retired tiery (R-prefix) v modes = archivovaná historie, ne aktuální tier.

    Jen report – retired tiery se nikdy nemazou ani nepřepisují.
    """
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        username = _username(p)
        raw_modes = p.get("modes")
        modes = raw_modes if isinstance(raw_modes, dict) else {}
        for kit, tier in sorted(modes.items()):
            if is_retired_tier(tier):
                raw = str(tier).strip()
                out.append(
                    _finding(
                        "retired_tiers_in_modes",
                        "warning",
                        f"🧓 **{username}** · **{kit}** má retired tier "
                        f"`{raw}` v modes (aktuální tiery). Patří do retired "
                        "historie – role se nesynchronizuje. (Nic se nemění "
                        "automaticky.)",
                    )
                )
    return out


def check_player_discord_ids(players: list) -> list:
    """Duplicitní discordId napříč hráči – jeden Discord účet = jeden hráč."""
    seen: dict[str, list] = {}
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        did = str(p.get("discordId", "") or "").strip()
        username = _username(p)
        if not did or not username:
            continue
        seen.setdefault(did, []).append(username)
    out = []
    for did, usernames in sorted(seen.items()):
        if len(usernames) > 1:
            out.append(
                _finding(
                    "duplicate_player_discord_ids",
                    "error",
                    f"🆔 Discord ID **{did}** patří {len(usernames)} hráčům: "
                    f"{', '.join(sorted(usernames))} – konflikt identity.",
                )
            )
    return out


def check_duplicate_kit_names(kits: list) -> list:
    """Case-insensitive duplicity v kits.json („MolePVP" + „molepvp").

    Report-only: sloučení dělá ``utils.get_kits`` (dedup), vstupní data se
    nemění automaticky – první výskyt je kanonický (display-case) název.
    """
    groups: dict[str, list] = {}
    for i, k in enumerate(kits or []):
        key = _normalize_key(k)
        if not key:
            continue
        groups.setdefault(key, []).append((i, str(k)))
    out = []
    for key, entries in sorted(groups.items()):
        if len(entries) > 1:
            names = ", ".join(f"`{n}`" for _, n in entries)
            out.append(
                _finding(
                    "duplicate_kit_names",
                    "warning",
                    f"🔁 Kit **{key}** je v kits.json {len(entries)}× s jiným case: "
                    f"{names}. Zůstane první výskyt (kanonický název), ostatní "
                    "se odvodí case-insensitive – oprava je bezeztrátová, "
                    "nic se nemění automaticky.",
                )
            )
    return out


def check_duplicate_modes_keys(players: list) -> list:
    """Duplicitní klíče jednoho kitu v modes hráče („MolePVP" i „molepvp").

    Vzniká historicky, když psali kity různé cog A s jiným case. Report-only:
    nové zápisy už jdou pod kanonickým názvem (services/results
    ``apply_result_to_players``), existující data se nemění automaticky.
    """
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        username = _username(p)
        modes = p.get("modes") if isinstance(p.get("modes"), dict) else {}
        groups: dict[str, list] = {}
        for key, value in modes.items():
            groups.setdefault(_normalize_key(key), []).append((str(key), value))
        for kit_low, entries in sorted(groups.items()):
            if len(entries) > 1:
                detail = ", ".join(f"`{k}` = `{v}`" for k, v in entries)
                out.append(
                    _finding(
                        "duplicate_modes_keys",
                        "warning",
                        f"🔁 **{username}** má kit **{kit_low}** {len(entries)}× "
                        f"v modes: {detail}. Sloučit na jeden klíč (návrh: "
                        "/edituser) – nic se nemění automaticky.",
                    )
                )
    return out


def check_eval_references(evals: dict, players: list, kits: list) -> list:
    player_igns = {_normalize_key(_username(p)) for p in (players or []) if isinstance(p, dict)}
    kit_keys = {_normalize_key(k) for k in (kits or [])}
    out = []
    for kit_key, bucket in sorted((evals or {}).items()):
        if not isinstance(bucket, dict):
            out.append(
                _finding(
                    "invalid_eval_references",
                    "warning",
                    f"🎓 Eval záznam pro kit **{kit_key}** není objekt "
                    f"(`{type(bucket).__name__}`) – poškozený bucket.",
                )
            )
            continue
        if kit_key not in kit_keys:
            out.append(
                _finding(
                    "invalid_eval_references",
                    "warning",
                    f"🎓 Eval záznam ukazuje na nezaregistrovaný kit "
                    f"**{kit_key}** ({len(bucket)} hráčů) – kit není v kits.json.",
                )
            )
        for ign in sorted(bucket):
            if ign not in player_igns:
                out.append(
                    _finding(
                        "invalid_eval_references",
                        "warning",
                        f"🎓 Eval IGN **{ign}** (kit **{kit_key}**) není "
                        "v players.json – odkaz na neexistujícího hráče.",
                    )
                )
    return out


def check_orphaned_tickets(tickets: dict, channel_exists=None) -> list:
    """Osamocené tickety: poškozené záznamy + otevřené tickety bez kanálu."""
    out = []
    for cid, ticket in sorted((tickets or {}).items()):
        cid = str(cid)
        if not isinstance(ticket, dict):
            out.append(
                _finding(
                    "orphaned_tickets",
                    "error",
                    f"🎟️ Ticket {cid} není objekt (`{type(ticket).__name__}`) – "
                    "poškozený záznam.",
                )
            )
            continue
        if not ticket.get("status") or not ticket.get("ownerId"):
            out.append(
                _finding(
                    "orphaned_tickets",
                    "error",
                    f"🎟️ Ticket **{cid}** nemá status/owner – poškozený záznam.",
                )
            )
            continue
        if ticket.get("id") and str(ticket.get("id")) != cid:
            out.append(
                _finding(
                    "orphaned_tickets",
                    "error",
                    f"🎟️ Ticket **{cid}** má `id` = **{ticket.get('id')}** – "
                    "klíč a id nesouhlasí.",
                )
            )
        if channel_exists is not None and ticket.get("status") != STATUS_CLOSED:
            try:
                gone = not bool(channel_exists(cid))
            except Exception:  # noqa: BLE001
                gone = False
            if gone:
                out.append(
                    _finding(
                        "orphaned_tickets",
                        "error",
                        f"🎟️ Otevřený ticket **{cid}** (kit **{ticket.get('kit')}**, "
                        f"owner **{ticket.get('ownerId')}**) – kanál už na serveru "
                        "neexistuje. Bezpečná oprava: jen ho zavřít (nic nemaže).",
                        repair={
                            "action": "close_ticket",
                            "label": f"Zavřít osamocený ticket {cid}",
                            "targets": [cid],
                        },
                    )
                )
    return out


def check_orphaned_results(results: dict, players: list, tickets: dict) -> list:
    player_igns = {
        _normalize_key(_username(p)) for p in (players or []) if isinstance(p, dict)
    }
    ticket_ids = {str(k) for k in (tickets or {}).keys()}
    out = []
    for rid, r in sorted((results or {}).items()):
        rid = str(rid)
        if not isinstance(r, dict):
            out.append(
                _finding(
                    "orphaned_results",
                    "error",
                    f"📋 Výsledek {rid} není objekt – poškozený záznam.",
                )
            )
            continue
        reasons = []
        ign = _normalize_key(r.get("ign"))
        if not ign or ign not in player_igns:
            reasons.append("hráč není v players.json")
        # HT Fight výsledky (/topresult, resultType=ht_fight) se NEPOVAŽUJÍ za
        # ticket výsledky: volné zápasy nemají ticketId vůbec, u ticket zápasu
        # se odkaz ověří jen když je ticketId vyplněný.
        kind = r.get("kind")
        is_ht_fight = kind == "ht_fight" or r.get("resultType") == "ht_fight"
        is_ticket = (not is_ht_fight) and (
            kind == "ticket" or not rid.startswith("queue-")
        )
        if is_ticket and str(r.get("ticketId") or "").strip() not in ticket_ids:
            reasons.append("reference na neexistující ticket")
        elif is_ht_fight and (r.get("ticketId")) and str(r["ticketId"]).strip() not in ticket_ids:
            reasons.append("reference na neexistující ticket")
        if reasons:
            out.append(
                _finding(
                    "orphaned_results",
                    "error",
                    f"📋 Výsledek **{rid}** ({r.get('ign') or '?'} · "
                    f"{r.get('kit') or '?'} · {r.get('newTier') or '?'}) – "
                    + ", ".join(reasons) + ". Záznam zůstává v historii, nemazat.",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Orchestrace (kontrola) + audit
# ---------------------------------------------------------------------------
async def run_datacheck(
    *,
    channel_exists=None,
    log_event: bool = True,
    actor_id=None,
    actor_name="",
    now: int = None,
) -> dict:
    """Přejde VŠECHNY místní databáze a vrátí přehled problémů (nic neopraví).

    ``channel_exists(channel_id: str) -> bool`` je volitelné rozhraní, které
    dodá Discord vrstva (existuje kanál ticketu?) – bez něj se kontrola kanálů
    přeskočí (jen strukturální kontrola).
    """
    if now is None:
        now = _now_ms()

    players = await store_read("players.json", [])
    if not isinstance(players, list):
        players = []
    evals = await store_read("evals.json", {})
    if not isinstance(evals, dict):
        evals = {}
    tickets = await store_read(HT_TICKETS_FILE, {})
    if not isinstance(tickets, dict):
        tickets = {}
    results = await store_read("ht_results.json", {})
    if not isinstance(results, dict):
        results = {}
    kit_roles = await store_read("kit_roles.json", {})
    if not isinstance(kit_roles, dict):
        kit_roles = {}
    kits = await store_read("kits.json", [])
    if not isinstance(kits, list):
        kits = []
    testers = await store_read("testers.json", [])
    if not isinstance(testers, list):
        testers = []

    findings: list = []
    findings += check_duplicate_players(players)
    findings += check_ticket_identities(tickets)
    findings += check_duplicate_testers(testers)
    findings += check_invalid_tiers(players)
    findings += check_kit_roles_conflicts(kit_roles)
    findings += check_missing_website_records(players)
    findings += check_retired_tiers_in_modes(players)
    findings += check_player_discord_ids(players)
    findings += check_duplicate_kit_names(kits)
    findings += check_duplicate_modes_keys(players)
    findings += check_eval_references(evals, players, kits)
    findings += check_orphaned_tickets(tickets, channel_exists=channel_exists)
    findings += check_orphaned_results(results, players, tickets)

    summary = {kind: 0 for kind in KINDS}
    for f in findings:
        summary[f["kind"]] += 1

    # Bezpečné opravy (jen ty, které nic nemažou).
    close_targets = set()
    tier_targets = []
    seen_tier = set()
    for f in findings:
        repair = f.get("repair")
        if not repair:
            continue
        for target in repair.get("targets", []):
            if repair["action"] == "close_ticket":
                close_targets.add(str(target))
            elif repair["action"] == "normalize_tier":
                key = (
                    target["username"].lower(),
                    _normalize_key(target["kit"]),
                    target["field"],
                    target["index"],
                )
                if key not in seen_tier:
                    seen_tier.add(key)
                    tier_targets.append(target)

    repairable = {
        "close_ticket": sorted(close_targets),
        "normalize_tier": tier_targets,
    }
    repairable_count = len(repairable["close_ticket"]) + len(repairable["normalize_tier"])

    result = {
        "ok": True,
        "has_issues": bool(findings),
        "total_findings": len(findings),
        "summary": summary,
        "findings": findings,
        "repairable": repairable,
        "repairable_count": repairable_count,
        "ts": now,
    }
    if log_event:
        await log_datacheck_event(
            actor_id=actor_id,
            actor_name=actor_name,
            mode="check",
            status="success",
            summary=summary,
            repairs={
                "close_ticket": len(repairable["close_ticket"]),
                "normalize_tier": len(repairable["normalize_tier"]),
            },
            errors=[],
            ts=now,
        )
    return result


# ---------------------------------------------------------------------------
# Bezpečné opravy (jen to, co nic nemaže) – po explicitním potvrzení
# ---------------------------------------------------------------------------
async def perform_repairs(
    *,
    close_ticket_ids=None,
    tier_fixes=None,
    actor_id=None,
    actor_name="",
    now: int = None,
) -> dict:
    """Aplikuje potvrzené bezpečné opravy (transakčně, audit-loggovaně).

    - ``close_ticket_ids`` – zavře otevřené osamocené tickety (záznam zůstává),
    - ``tier_fixes``       – normalizuje zápisy tierů na kanonickou podobu
      (players.json: modes/history).

    Nikdy nic nemaže. Vrací přehled provedených oprav a chyb.
    """
    close_ids = sorted({str(x) for x in (close_ticket_ids or [])})
    fixes = list(tier_fixes or [])
    if not close_ids and not fixes:
        return {
            "ok": False,
            "message": "Nic k opravě.",
            "closed": [],
            "normalized": [],
            "errors": [],
        }
    if now is None:
        now = _now_ms()

    files = []
    if close_ids:
        files += [HT_TICKETS_FILE]
    if fixes:
        files += ["players.json"]

    async def _run(tx):
        closed = []
        normalized = []
        errors = []

        # 1) Zavřít osamocené tickety (záznam se NEMAŽE, jen status).
        if close_ids:
            tickets = tx.get(HT_TICKETS_FILE, {})
            if not isinstance(tickets, dict):
                tickets = {}
            for cid in close_ids:
                ticket = tickets.get(cid)
                if not isinstance(ticket, dict):
                    errors.append({"target": cid, "error": "ticket neexistuje"})
                    continue
                if ticket.get("status") == STATUS_CLOSED:
                    closed.append({"id": cid, "skipped": True})
                    continue
                ticket["status"] = STATUS_CLOSED
                ticket["closedAt"] = now
                tickets[cid] = ticket
                closed.append({"id": cid, "skipped": False})
            tx.set(HT_TICKETS_FILE, tickets)

        # 2) Normalizovat tiery v players.json (bezeztrátově).
        if fixes:
            players = tx.get("players.json", [])
            if not isinstance(players, list):
                players = []
            for fix in fixes:
                try:
                    username = str(fix["username"] or "").strip()
                    kit = str(fix["kit"] or "").strip()
                    field = fix.get("field")
                    new_value = str(fix.get("to") or "").strip().upper()
                    if not username or not kit or not new_value:
                        errors.append({"target": fix, "error": "neplatné pole opravy"})
                        continue
                    player = next(
                        (
                            p
                            for p in players
                            if isinstance(p, dict)
                            and str(p.get("username", "")).strip().lower()
                            == username.lower()
                        ),
                        None,
                    )
                    if player is None:
                        errors.append({"target": fix, "error": "hráč v players.json není"})
                        continue
                    if field == "modes":
                        modes = player.setdefault("modes", {})
                        if modes.get(kit) != new_value:
                            modes[kit] = new_value
                            normalized.append({"target": fix, "skipped": False})
                        else:
                            normalized.append({"target": fix, "skipped": True})
                    elif field == "history":
                        history = player.setdefault("history", {})
                        bucket = history.setdefault(kit, [])
                        index = fix.get("index")
                        if isinstance(index, int) and 0 <= index < len(bucket):
                            bucket[index]["tier"] = new_value
                            normalized.append({"target": fix, "skipped": False})
                        else:
                            errors.append({"target": fix, "error": "pozice v historii není"})
                    else:
                        errors.append({"target": fix, "error": "neznámé pole"})
                except Exception as err:  # noqa: BLE001
                    log.exception("Chyba při normalizaci tieru: %s", err)
                    errors.append({"target": fix, "error": str(err)})
            tx.set("players.json", players)

        return {
            "closed": closed,
            "normalized": normalized,
            "errors": errors,
        }

    outcome = await transaction(tuple(files), _run)
    errors = outcome.get("errors", [])
    result = {
        "ok": not errors,
        "message": (
            f"✅ Bezpečné opravy aplikovány: {len(outcome['closed'])} ticketů "
            f"zavřeno, {len(outcome['normalized'])} tierů normalizováno."
            if not errors
            else "⚠️ Opravy proběhly s chybami – viz detaily."
        ),
        "closed": outcome.get("closed", []),
        "normalized": outcome.get("normalized", []),
        "errors": errors,
        "ts": now,
    }
    await log_datacheck_event(
        actor_id=actor_id,
        actor_name=actor_name,
        mode="repair",
        status="success" if not errors else "failure",
        summary={},
        repairs={
            "closed": len(outcome.get("closed", [])),
            "normalized": len(outcome.get("normalized", [])),
        },
        errors=[f"{e.get('target')}: {e.get('error')}" for e in errors],
        ts=now,
    )
    return result


# ---------------------------------------------------------------------------
# Auditní log (data/datacheck_log.json, append-only, restart-safe)
# ---------------------------------------------------------------------------
async def log_datacheck_event(
    *,
    actor_id=None,
    actor_name="",
    mode: str,
    status: str,
    summary: dict = None,
    repairs: dict = None,
    errors: list = None,
    ts: int = None,
) -> dict:
    if ts is None:
        ts = _now_ms()
    entry = {
        "ts": ts,
        "mode": mode,
        "status": status,
        "summary": dict(summary or {}),
        "repairs": dict(repairs or {}),
        "errors": list(errors or []),
    }
    if actor_id is not None:
        entry["actorId"] = str(actor_id)
        entry["actorName"] = actor_name or ""

    async def _run(tx):
        entries = tx.get(DATACHECK_LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        tx.set(DATACHECK_LOG_FILE, entries)
        return entry

    return await transaction((DATACHECK_LOG_FILE,), _run)


async def get_datacheck_log() -> list:
    entries = await store_read(DATACHECK_LOG_FILE, [])
    return [e for e in entries if isinstance(e, dict)]
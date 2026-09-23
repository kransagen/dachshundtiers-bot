"""Synchronizace Discord tier rolí s kanonickou players.json (bez discord.py).

Požadavky Phase 4:
- porovná Discord tier role (``data/kit_roles.json``) s kanonickou databází
  hráčů (``data/players.json``),
- detekuje 6 kategorií rozdílů,
- NIKDY neřeší konflikty automaticky – vždy se nejdřív ukáže přehled
  (``/playersync preview``) a změny se aplikují JEN po explicitním potvrzení
  (``/playersync apply``),
- každé použití se zapisuje do ``data/playersync_log.json`` (audit, append-only).

Detekované kategorie (názvy podle požadavku):
- ``missing_role``    – hráč má v DB tier, ale roli na serveru nemá
                        (navrhuje se: přidat roli),
- ``wrong_role``      – hráč drží roli tieru, který neodpovídá DB
                        (navrhuje se: roli odebrat),
- ``multiple_roles``  – hráč drží víc tier rolí stejného kitu najednou
                        (jen report; konkrétní odebrané role řeší ``wrong_role``),
- ``unknown_player``  – člen drží tier roli, ale v players.json žádný takový
                        hráč není (navrhuje se: roli odebrat),
- ``missing_player``  – hráč v DB má tier pro kit, ale na serveru není žádný
                        člen pod tímto jménem (jen report – oprava není možná),
- ``invalid_tier``    – tier v DB pro kit není namapovaný v kit_roles.json
                        (jen report – oprava je ruční).

Analýza je čistá funkce nad normalizovanými daty (členové bez discord.py)::

    members = [
        {
            "id": "123",
            "name": "AliceMC",              # primární jméno pro hlášky
            "names": ["AliceMC", "alice"],  # kandidáti pro párování s IGN
            "role_ids": {"111", "222"},     # ID rolí, které člen drží
        },
        ...
    ]
    analyze_sync(players, members, roles_map, kit_display)

Člen ↔ hráč se páruje podle jména (IGN = nick / display name / username,
case-insensitive), stejně jako u ``/checkweb``. Poškozená data se přeskakují.
"""

import logging
import time

from services.store import read as store_read, transaction

log = logging.getLogger("dachshundtiers")

PLAYERSYNC_LOG_FILE = "playersync_log.json"

# Pořadí kategorií pro souhrn / zobrazení
KINDS = (
    "missing_role",
    "wrong_role",
    "multiple_roles",
    "unknown_player",
    "missing_player",
    "invalid_tier",
)

KIND_LABELS = {
    "missing_role": "➕ Chybějící role",
    "wrong_role": "✏️ Špatné role",
    "multiple_roles": "🔁 Více tier rolí",
    "unknown_player": "👤 Neznámí hráči",
    "missing_player": "🚫 Chybějící hráči",
    "invalid_tier": "❌ Neplatné tiery",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Normalizovaní členové
# ---------------------------------------------------------------------------
def make_member(member_id, name, role_ids, extra_names=None) -> dict:
    """Sestaví normalizovaného člena pro analýzu (viz docstring modulu)."""
    names = set()
    primary = ""
    if name is not None and str(name).strip():
        primary = str(name).strip()
        names.add(primary)
    for n in extra_names or ():
        if n is not None and str(n).strip():
            names.add(str(n).strip())
    if not primary and names:
        primary = sorted(names)[0]
    return {
        "id": str(member_id),
        "name": primary,
        "names": sorted(names),
        "role_ids": {str(r) for r in (role_ids or [])},
    }


# ---------------------------------------------------------------------------
# Analýza (čistá funkce – nic neaplikuje)
# ---------------------------------------------------------------------------
def _finding(
    kind,
    *,
    member_id=None,
    member_name="",
    ign="",
    kit="",
    kit_key="",
    expected_tier=None,
    actual_tier=None,
    role_id=None,
    message="",
    action=None,
) -> dict:
    return {
        "kind": kind,
        "member_id": str(member_id) if member_id is not None else None,
        "member_name": member_name or "",
        "ign": ign or "",
        "kit": kit,
        "kit_key": kit_key,
        "expected_tier": expected_tier,
        "actual_tier": actual_tier,
        "role_id": str(role_id) if role_id is not None else None,
        "message": message,
        "action": action,
    }


def _mode_value(modes: dict, kit_key: str):
    """Hodnota módu pro kit (case-insensitive) – (klíč, tier), jinak (None, None)."""
    for key, value in (modes or {}).items():
        if str(key).strip().lower() == kit_key:
            return key, value
    return None, None


def _match_player(players, names) -> dict | None:
    """Najde hráče podle některého z jmen (case-insensitive), jinak None."""
    keys = {
        str(n).strip().lower() for n in (names or []) if n and str(n).strip()
    }
    if not keys:
        return None
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        username = str(p.get("username", "") or "").strip().lower()
        if username and username in keys:
            return p
    return None


def analyze_sync(players, members, roles_map, kit_display=None) -> dict:
    """Porovná role a DB a vrátí nálezy + navržené akce (neupravuje data).

    Vrací::
        {
          "summary":        {kind: počet},  # všechny kategorie, i 0
          "checked":        počet zkontrolovaných párů (člen × kit),
          "findings":       [nález, ...],   # viz _finding
          "actions":        [akce, ...]     # deduplikované = to, co by se potvrdilo,
          "has_actions":    bool,
          "has_issues":     bool(findings),
          "fingerprint":    otisk akcí pro porovnání preview vs potvrzení,
        }
    """
    kit_display = kit_display or {}
    players = sorted(
        (p for p in (players or []) if isinstance(p, dict)),
        key=lambda p: str(p.get("username", "") or "").lower(),
    )
    members = sorted(
        (m for m in (members or []) if isinstance(m, dict) and str(m.get("id", ""))),
        key=lambda m: str(m.get("id", "")),
    )

    findings: list = []
    summary = {kind: 0 for kind in KINDS}
    checked = 0

    for kit_key, kit_map in sorted(
        (roles_map or {}).items(), key=lambda kv: str(kv[0])
    ):
        kit_key = str(kit_key).strip().lower()
        if not kit_key or not isinstance(kit_map, dict):
            continue

        # Platné mapování tier → role (přeskakujeme poškozené záznamy)
        tier_to_role = {}
        role_to_tier = {}
        for tier, role_id in kit_map.items():
            tier_up = str(tier).strip().upper()
            rid = str(role_id).strip()
            if not tier_up or not rid.isdigit():
                continue
            tier_to_role[tier_up] = rid
            role_to_tier[rid] = tier_up
        if not tier_to_role:
            continue

        display = kit_display.get(kit_key)
        kit_name = str(display).strip() if display else kit_key.capitalize()

        # Index členů podle všech jmen (první výskyt vyhrává)
        members_by_name = {}
        for m in members:
            for n in m.get("names") or []:
                key = str(n).strip().lower()
                if key and key not in members_by_name:
                    members_by_name[key] = m

        # Očekávaný tier z DB (pro tento kit) pro každého člena
        expected_by_member: dict = {}
        for p in players:
            ign = str(p.get("username", "") or "").strip()
            if not ign:
                continue
            _, mode_val = _mode_value(p.get("modes") or {}, kit_key)
            if mode_val is None or not str(mode_val).strip():
                continue
            tier = str(mode_val).strip().upper()
            if tier not in tier_to_role:
                findings.append(
                    _finding(
                        "invalid_tier",
                        ign=ign,
                        kit=kit_name,
                        kit_key=kit_key,
                        expected_tier=tier,
                        message=(
                            f"❌ **{ign}** má v players.json tier **{tier}** pro "
                            f"**{kit_name}**, ale v kit_roles.json není namapovaný – "
                            "doplň ho přes `/setkitrole`."
                        ),
                    )
                )
                summary["invalid_tier"] += 1
                continue
            member = members_by_name.get(ign.lower())
            if member is None:
                findings.append(
                    _finding(
                        "missing_player",
                        ign=ign,
                        kit=kit_name,
                        kit_key=kit_key,
                        expected_tier=tier,
                        message=(
                            f"🚫 **{ign}** má v players.json tier **{tier}** pro "
                            f"**{kit_name}**, ale na serveru není žádný člen pod "
                            "tímto jménem."
                        ),
                    )
                )
                summary["missing_player"] += 1
                continue
            expected_by_member[str(member["id"])] = (tier, tier_to_role[tier], ign)

        # Projití členů: co se neshoduje s DB
        for m in members:
            mid = str(m["id"])
            mname = str(m.get("name", "") or "")
            role_ids = set(m.get("role_ids") or [])
            held_ids = sorted(rid for rid in role_ids if rid in role_to_tier)
            held_tiers = sorted({role_to_tier[rid] for rid in held_ids})
            exp = expected_by_member.get(mid)

            if not held_ids and exp is None:
                continue
            checked += 1

            if len(held_tiers) > 1:
                findings.append(
                    _finding(
                        "multiple_roles",
                        member_id=mid,
                        member_name=mname,
                        ign=exp[2] if exp else "",
                        kit=kit_name,
                        kit_key=kit_key,
                        expected_tier=exp[0] if exp else None,
                        actual_tier=", ".join(held_tiers),
                        message=(
                            f"🔁 **{mname or mid}** drží víc tier rolí kitu "
                            f"**{kit_name}** najednou: {', '.join(held_tiers)}."
                        ),
                    )
                )
                summary["multiple_roles"] += 1

            if exp is None:
                if not held_ids:
                    continue
                # Člen drží tier roli, ale DB nemá pro tento kit žádný tier –
                # buď je hráč v DB bez tieru (špatná role), nebo v DB vůbec není.
                player = _match_player(players, m.get("names") or [])
                for rid in held_ids:
                    if player is not None:
                        findings.append(
                            _finding(
                                "wrong_role",
                                member_id=mid,
                                member_name=mname,
                                ign=str(player.get("username", "") or ""),
                                kit=kit_name,
                                kit_key=kit_key,
                                actual_tier=role_to_tier[rid],
                                role_id=rid,
                                message=(
                                    f"✏️ **{mname or mid}** drží roli "
                                    f"**{role_to_tier[rid]}** pro **{kit_name}**, ale "
                                    "v players.json nemá pro tento kit žádný tier."
                                ),
                                action={
                                    "op": "remove",
                                    "member_id": mid,
                                    "member_name": mname,
                                    "role_id": rid,
                                    "kit": kit_name,
                                    "kit_key": kit_key,
                                    "tier": role_to_tier[rid],
                                },
                            )
                        )
                    else:
                        findings.append(
                            _finding(
                                "unknown_player",
                                member_id=mid,
                                member_name=mname,
                                kit=kit_name,
                                kit_key=kit_key,
                                actual_tier=role_to_tier[rid],
                                role_id=rid,
                                message=(
                                    f"👤 **{mname or mid}** drží roli "
                                    f"**{role_to_tier[rid]}** pro **{kit_name}**, ale "
                                    "v players.json žádný takový hráč není."
                                ),
                                action={
                                    "op": "remove",
                                    "member_id": mid,
                                    "member_name": mname,
                                    "role_id": rid,
                                    "kit": kit_name,
                                    "kit_key": kit_key,
                                    "tier": role_to_tier[rid],
                                },
                            )
                        )
                summary["wrong_role" if player is not None else "unknown_player"] += len(held_ids)
                continue

            expected_tier, expected_role, ign = exp
            if expected_role not in held_ids:
                findings.append(
                    _finding(
                        "missing_role",
                        member_id=mid,
                        member_name=mname,
                        ign=ign,
                        kit=kit_name,
                        kit_key=kit_key,
                        expected_tier=expected_tier,
                        role_id=expected_role,
                        message=(
                            f"➕ **{mname or mid}** ({ign}) má v players.json tier "
                            f"**{expected_tier}** pro **{kit_name}**, ale roli na "
                            "serveru nemá."
                        ),
                        action={
                            "op": "add",
                            "member_id": mid,
                            "member_name": mname,
                            "role_id": expected_role,
                            "kit": kit_name,
                            "kit_key": kit_key,
                            "tier": expected_tier,
                        },
                    )
                )
                summary["missing_role"] += 1

            for rid in held_ids:
                if rid == expected_role:
                    continue
                findings.append(
                    _finding(
                        "wrong_role",
                        member_id=mid,
                        member_name=mname,
                        ign=ign,
                        kit=kit_name,
                        kit_key=kit_key,
                        expected_tier=expected_tier,
                        actual_tier=role_to_tier[rid],
                        role_id=rid,
                        message=(
                            f"✏️ **{mname or mid}** ({ign}) drží roli "
                            f"**{role_to_tier[rid]}**, ale v players.json má "
                            f"**{expected_tier}**."
                        ),
                        action={
                            "op": "remove",
                            "member_id": mid,
                            "member_name": mname,
                            "role_id": rid,
                            "kit": kit_name,
                            "kit_key": kit_key,
                            "tier": role_to_tier[rid],
                        },
                    )
                )
                summary["wrong_role"] += 1

    actions = build_actions(findings)
    return {
        "summary": summary,
        "checked": checked,
        "findings": findings,
        "actions": actions,
        "has_actions": bool(actions),
        "has_issues": bool(findings),
        "fingerprint": fingerprint(actions),
    }


# ---------------------------------------------------------------------------
# Akce a otisk (pro potvrzení apply)
# ---------------------------------------------------------------------------
def build_actions(findings) -> list:
    """Zploští nálezy na deduplikovaný seznam akcí (add/remove roli)."""
    actions = []
    seen = set()
    for f in (findings or []):
        action = f.get("action") if isinstance(f, dict) else None
        if not action:
            continue
        key = (
            str(action.get("member_id", "")),
            str(action.get("role_id", "")),
            action.get("op"),
        )
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    return actions


def fingerprint(actions) -> str:
    """Deterministický otisk plánu akcí – porovnání preview vs potvrzení.

    Na pořadí akcí nezáleží; liší-li se jakákoliv akce, otisk se liší.
    """
    return "|".join(
        sorted(
            f"{a.get('op')}@{a.get('member_id')}@{a.get('role_id')}"
            for a in (actions or [])
        )
    )


# ---------------------------------------------------------------------------
# Auditní log (data/playersync_log.json, append-only, restart-safe)
# ---------------------------------------------------------------------------
async def log_playersync_event(
    *,
    actor_id,
    actor_name,
    mode: str,
    summary=None,
    actions=None,
    applied=None,
    ts: int = None,
) -> dict:
    """Přidá záznam do auditu (``mode`` = ``preview`` | ``apply``).

    - ``preview`` → uloží i ``plannedActions`` (co se plánovalo),
    - ``apply``   → uloží ``applied`` s výsledkem každé akce (ok/error).

    Atomický (transaction přes jeden soubor) a restart-safe. Vrací záznam.
    """
    if ts is None:
        ts = _now_ms()
    entry: dict = {
        "ts": ts,
        "mode": mode,
        "actorId": str(actor_id),
        "actorName": actor_name or "",
        "summary": dict(summary or {}),
    }
    if mode == "apply":
        entry["applied"] = list(applied or [])
    else:
        entry["plannedActions"] = list(actions or [])

    async def _run(tx):
        entries = tx.get(PLAYERSYNC_LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        tx.set(PLAYERSYNC_LOG_FILE, entries)
        return entry

    return await transaction((PLAYERSYNC_LOG_FILE,), _run)


async def get_playersync_log() -> list:
    """Všechny záznamy auditu v pořadí zápisu (chronologicky)."""
    entries = await store_read(PLAYERSYNC_LOG_FILE, [])
    return [e for e in entries if isinstance(e, dict)]
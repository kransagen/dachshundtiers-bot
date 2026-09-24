"""Centrální admin editor hráče – /edituser (čistá logika + transakce, bez discord.py).

Jeden editor, žádné paralelní systémy: všechno se recykluje z existujících
služeb projektu:

    - identita          → services/player_identity (Discord ID je primární),
    - tiery             → HT3_TIER_LADDER + services/datacheck.canonical_tier
                          (kompatibilita s legacy zápisy jako „LT3 EVAL“),
    - retired tiery     → konvence R-prefixu z services/playersync,
    - Discord role      → services/role_sync.analyze_role_sync (jeden kit),
    - cooldowny         → services/queue_service.cooldown_remaining +
                          stejné soubory/klíče jako join/result (cooldowns.json,
                          ht3_cooldowns.json),
    - web/GitHub        → services/websync.sync_website (jeden zapisovatel webu),
    - transakce/locks   → services/store.transaction (strict mode – poškozený
                          JSON se nikdy nepřepíše, DataCorruptionError),
    - audit             → data/edituser_log.json (append-only, stejný vzor jako
                          playersync_log / websync_log / datacheck_log).

Změny identity NIKDY neslučují hráče a jsou idempotentní:
  - opakovaná stejná změna → ``unchanged`` (žádný duplicitní záznam, žádný
    zbytečný audit, žádný push na web),
  - změna Discord ID NIKDY nevytvoří nového hráče; data (tiery, historie,
    výsledky, cooldowny, tickety, statistiky, metadata) zůstávají u hráče
    a klíče odkazující na staré ID se přemapují,
  - změna IGN přejmenuje záznam; IGN patřící jinému Discord ID = konflikt.

Invarianty tierů (kodifikované):
  - retired tier NIKDY nepřepíše aktuální tier – jediná povolená „retire“
    změna je archivace PŘESNĚ stejné hodnoty (HT3 → RHT3),
  - aktuální tier NIKDY nepřepíše retired tier (archivovanou historii) –
    povýšení jde přes /result, které zapisuje modes + historii najednou,
  - historie (``history``) se editorem NIKDY nemění ani nemaže.

Stav se nikdy nemutuje na místě – čisté funkce pracují s kopiemi (stejný
styl jako services/results.apply_result_to_players).
"""

import logging
import time

from services import datacheck
from services.player_identity import (
    CLAIM_UNCHANGED,
    PlayerIdentityConflict,
    claim_ign,
    find_by_discord_id,
    find_by_ign,
)
from services.playersync import is_retired_tier
from services.queue_service import cooldown_remaining
from services.results import HT_RESULTS_FILE
from services.role_sync import analyze_role_sync
from services.store import read as store_read, transaction
from services.tickets import HT3_TIER_LADDER, HT_TICKETS_FILE
from storage import DataCorruptionError

log = logging.getLogger("dachshundtiers")

EDITUSER_LOG_FILE = "edituser_log.json"

# Stavy výsledků operace (pro report SUCCESS / PARTIAL SUCCESS / FAILURE).
STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL SUCCESS"
STATUS_FAILURE = "FAILURE"

# Výsledky aplikace jedné změny.
OUTCOME_CHANGED = "changed"
OUTCOME_UNCHANGED = "unchanged"

# Tournament tier kity (S/A/B/C/D/E) patří do známého vesmíru tierů
# (datacheck.KNOWN_TIERS) – select editoru nabízí žebříček + retired varianty.
RETIRED_PREFIX = "R"


class InvalidTierEdit(ValueError):
    """Neplatná / zakázaná změna tieru (např. retired přes aktuální tier)."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _copy_players(players) -> list:
    """Hluboká kopie hráčských záznamů – vstup se nikdy nemutuje."""
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            out.append(p)
            continue
        cloned = dict(p)
        if isinstance(p.get("modes"), dict):
            cloned["modes"] = dict(p["modes"])
        if isinstance(p.get("history"), dict):
            cloned["history"] = {
                k: (list(v) if isinstance(v, list) else v)
                for k, v in p["history"].items()
            }
        out.append(cloned)
    return out


def _locate(players, ref) -> dict | None:
    """Najde kopii editovaného záznamu (podle Discord ID, jinak IGN)."""
    did = str((ref or {}).get("discordId") or "").strip()
    if did.isdigit():
        return find_by_discord_id(players, did)
    username = str((ref or {}).get("username") or "").strip()
    if not username:
        return None
    matches = [
        p
        for p in (players or [])
        if isinstance(p, dict)
        and str(p.get("username") or "").strip().lower() == username.lower()
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_mode_key(modes, kit_key: str) -> str | None:
    """Klíč v ``modes`` pro kit (case-insensitive), nebo None.

    /result zapisuje tiery pod původním názvem kitu (může být display-case,
    např. „RandomPot"), kit_roles.json používá lowercase – editor najde
    existující klíč, aby nevznikla duplicita („randompot" i „RandomPot").
    """
    kit_low = (kit_key or "").strip().lower()
    if not kit_low or not isinstance(modes, dict):
        return None
    for k in modes:
        if str(k).strip().lower() == kit_low:
            return k
    return None


def _mode_tier(modes, kit_key: str) -> str:
    """Hodnota tieru kitu v ``modes`` (case-insensitive), nebo ''."""
    key = _resolve_mode_key(modes, kit_key)
    if key is None:
        return ""
    return str(modes.get(key) or "").strip().upper()


# ---------------------------------------------------------------------------
# Tier výběry (select menu) – žádný free-text
# ---------------------------------------------------------------------------
def current_tier_choices() -> list:
    """Aktuální (ne-retired) tier hodnoty pro select – známý vesmír projektu."""
    out = []
    for t in HT3_TIER_LADDER:
        if t == "LT3E":  # virtuální status (LT3 + eval) – není reálný tier
            continue
        out.append(t)
    for t in ("S", "A", "B", "C", "D", "E"):
        if t not in out:
            out.append(t)
    return out


def retired_tier_choices() -> list:
    """Retired tier hodnoty pro select (R-prefix konvence z playersync)."""
    return [RETIRED_PREFIX + t for t in current_tier_choices()]


def normalize_tier_choice(value, *, retired: bool) -> str | None:
    """Kanonická podoba uloženého tieru z výběru, nebo None pro neplatný.

    Legacy reprezentace („lt3 eval“, „ht3“, …) se normalizují přes
    services/datacheck.canonical_tier – kompatibilita s existujícím systémem.
    """
    val = str(value or "").strip().upper()
    base = val[1:] if val.startswith(RETIRED_PREFIX) and len(val) > 1 else val
    canon = datacheck.canonical_tier(base) if base else None
    if canon is None or canon == "LT3E":
        return None
    return RETIRED_PREFIX + canon if retired else canon


# ---------------------------------------------------------------------------
# Identita – změny (čisté funkce, kopie; konflikty zvedají PlayerIdentityConflict)
# ---------------------------------------------------------------------------
def change_player_ign(players, ref, new_ign: str):
    """Přejmenuje IGN hráče (identita zůstává, historie zůstává).

    - hráč s Discord ID → services/player_identity.claim_ign (přejmenování /
      adopce nikdy neslučují, IGN jiného účtu = konflikt),
    - hráč BEZ Discord ID (legacy) → přejmenování konkrétního záznamu,
      IGN patřící jinému záznamu = konflikt.

    Vrací ``(players, target, outcome)``; outcome je CLAIM_* / "renamed".
    """
    players = _copy_players(players)
    ign_clean = (new_ign or "").strip()
    if not ign_clean:
        raise PlayerIdentityConflict("IGN je prázdné – nelze změnit.")
    target = _locate(players, ref)
    if target is None:
        raise PlayerIdentityConflict("Hráč nebyl v players.json nalezen.")

    did = str(target.get("discordId") or "").strip()
    if did.isdigit():
        players, renamed, outcome = claim_ign(
            players, discord_id=did, ign=ign_clean
        )
        return players, renamed, outcome

    other = find_by_ign(players, ign_clean)
    if other is not None and other is not target:
        raise PlayerIdentityConflict(
            f"IGN `{ign_clean}` patří jinému záznamu "
            f"(`{other.get('username') or '?'}`) – přejmenování se odmítá."
        )
    if str(target.get("username") or "").strip().lower() == ign_clean.lower():
        return players, target, CLAIM_UNCHANGED
    target["username"] = ign_clean
    return players, target, "renamed"


def change_player_discord(players, ref, new_discord_id: str):
    """Změní Discord ID hráče (NIKDY nevytvoří nového hráče).

    - nové ID patří jinému hráči → PlayerIdentityConflict (nikdy neslučovat),
    - prázdné / nečíselné ID → PlayerIdentityConflict,
    - opakovaná stejná změna → CLAIM_UNCHANGED (idempotence).

    Vrací ``(players, target, outcome)``.
    """
    did_clean = str(new_discord_id or "").strip()
    if not did_clean.isdigit():
        raise PlayerIdentityConflict(
            "Nové Discord ID musí být číslo (Discord ID uživatele)."
        )
    if not did_clean.isdigit() or len(did_clean) < 15:
        raise PlayerIdentityConflict(
            "Nové Discord ID vypadá podezřele (Discord ID má ~17–19 číslic)."
        )
    players = _copy_players(players)
    target = _locate(players, ref)
    if target is None:
        raise PlayerIdentityConflict("Hráč nebyl v players.json nalezen.")

    other = find_by_discord_id(players, did_clean)
    if other is not None and other is not target:
        raise PlayerIdentityConflict(
            f"Discord ID `{did_clean}` už patří hráči "
            f"`{other.get('username') or '?'}` – identitní konflikt, "
            "sloučení hráčů se odmítá."
        )
    old = str(target.get("discordId") or "").strip()
    if old == did_clean:
        return players, target, CLAIM_UNCHANGED
    target["discordId"] = did_clean
    return players, target, "changed"


# ---------------------------------------------------------------------------
# Tiers – změna tieru kitu (čistá funkce)
# ---------------------------------------------------------------------------
def change_kit_tier(players, ref, kit_key: str, new_tier: str, *, retired: bool):
    """Změní hodnotu tieru jednoho kitu v ``modes``.

    Historie se NIKDY nemění ani nemaže. Invarianty:
      - retired tier NIKDY nepřepíše aktuální tier,
      - aktuální tier NIKDY nepřepíše retired tier (archivovanou historii),
      - jediná povolená archivace = přesně stejná hodnota (HT3 → RHT3),
      - retired → retired změna je povolená (RHT3 → RLT2).

    Vrací ``(players, target, old_value, outcome)``.
    """
    stored = normalize_tier_choice(new_tier, retired=retired)
    if stored is None:
        raise InvalidTierEdit(f"Neplatný tier `{new_tier}`.")
    players = _copy_players(players)
    target = _locate(players, ref)
    if target is None:
        raise InvalidTierEdit("Hráč nebyl v players.json nalezen.")
    kit_key = (kit_key or "").strip().lower()
    modes = target.setdefault("modes", {})
    existing_key = _resolve_mode_key(modes, kit_key)
    old_raw = modes.get(existing_key) if existing_key else modes.get(kit_key)
    old = str(old_raw or "").strip().upper()

    if old == stored:
        return players, target, old_raw, OUTCOME_UNCHANGED

    if retired:
        if old and not is_retired_tier(old):
            base = stored[len(RETIRED_PREFIX):] if stored.startswith(RETIRED_PREFIX) else stored
            if base != old:
                raise InvalidTierEdit(
                    f"Retired tier `{stored}` by přepsal aktuální tier `{old}` "
                    "kitu – retired tier se NESMÍ překrýt s aktuálním. "
                    f"Archivovat lze jen přesně stejnou hodnotu (`R{old}`)."
                )
    else:
        if old and is_retired_tier(old):
            raise InvalidTierEdit(
                f"Kit má retired tier `{old}` (archivovaná historie) – nejde ho "
                "přepsat aktuálním tierem. Aktuální tier se zapisuje přes "
                "`/result` (modes + historie najednou)."
            )

    modes[existing_key or kit_key] = stored
    return players, target, old_raw, OUTCOME_CHANGED


# ---------------------------------------------------------------------------
# Discord role – plán změn přes stávající RoleSyncService (jeden kit)
# ---------------------------------------------------------------------------
def plan_role_sync(*, player, member, roles_map, kit_key, kit_display=None) -> dict:
    """Plán změn tier rolí po úpravě tieru (RoleSyncService, jen tento kit).

    Vrací ``{"actions": [...], "synced": bool, "note": str}``:
      - retired tier → role se NEsynchronizují (retired role se zachovávají),
      - aktuální tier bez namapované role → role se NEmění (tier v DB zůstává –
        absence namapované role nikdy nesmaže tier),
      - jinak akce z analyze_role_sync (add nové role / remove starých
        aktuálních rolí); retired role nikdy nepatří mezi odstraňované.
    """
    kit_display = kit_display or {}
    kit_key = (kit_key or "").strip().lower()
    tier = _mode_tier((player or {}).get("modes") or {}, kit_key)
    if not tier:
        return {"actions": [], "synced": False, "note": "hráč nemá pro kit žádný tier."}
    if is_retired_tier(tier):
        return {
            "actions": [],
            "synced": False,
            "note": "retired tier – Discord role se nesynchronizují "
            "(retired role zůstávají zachované).",
        }
    if not isinstance(member, dict) or not str(member.get("id") or ""):
        return {
            "actions": [],
            "synced": False,
            "note": "člen není na serveru – Discord role se nesynchronizují.",
        }
    kit_map = (roles_map or {}).get(kit_key)
    if not isinstance(kit_map, dict):
        return {"actions": [], "synced": False, "note": "kit nemá namapované role."}
    current_map = {
        k: v for k, v in kit_map.items() if not is_retired_tier(str(k))
    }
    if tier not in current_map:
        return {
            "actions": [],
            "synced": False,
            "note": f"tier `{tier}` nemá namapovanou roli – role se nemění "
            "(tier v DB zůstává beze změny).",
        }
    analysis = analyze_role_sync(
        [player],
        [member],
        {kit_key: current_map},
        {kit_key: kit_display.get(kit_key, kit_key.capitalize())},
    )
    return {
        "actions": analysis["actions"],
        "synced": True,
        "note": "",
    }


# ---------------------------------------------------------------------------
# Cooldowny – stávající výpočty a soubory (žádná druhá implementace)
# ---------------------------------------------------------------------------
def format_duration(ms) -> str:
    """Čitelný popis zbývajícího času (dny/hodiny/minuty)."""
    if ms is None or ms <= 0:
        return "žádný"
    days = int(ms // (24 * 60 * 60 * 1000))
    hours = int((ms % (24 * 60 * 60 * 1000)) // (60 * 60 * 1000))
    minutes = int((ms % (60 * 60 * 1000)) // (60 * 1000))
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def ht3_cooldown_remaining(ht3_cooldowns, user_id: str, kit_key: str, now: int):
    """Zbývající ms HT3+ cooldownu pro kit, nebo None."""
    if not isinstance(ht3_cooldowns, dict):
        return None
    expiry = (ht3_cooldowns.get(user_id) or {}).get(kit_key)
    if expiry is None:
        return None
    remaining = int(expiry) - now
    return remaining if remaining > 0 else None


def cooldown_snapshot(cooldowns, ht3_cooldowns, user_id: str, now: int, queue_cooldown_ms: int) -> dict:
    """Stručný přehled cooldownů hráče (pro zobrazení)."""
    queue_remaining = cooldown_remaining(cooldowns, user_id, now, queue_cooldown_ms)
    ht3 = {
        str(kit): format_duration(
            ht3_cooldown_remaining(ht3_cooldowns, user_id, str(kit), now)
        )
        for kit in sorted((ht3_cooldowns.get(user_id) or {}).keys())
    }
    return {
        "queue_remaining": queue_remaining,
        "queue_last": (cooldowns or {}).get(user_id),
        "ht3": ht3,
    }


def apply_cooldown_edit(
    tx,
    *,
    player_id: str,
    action: str,
    kit_key: str = None,
    now: int,
    queue_cooldown_ms: int,
    ht3_cooldown_ms: int,
):
    """Aplikuje změnu cooldownu uvnitř transakce.

    Vrací ``(old_desc, new_desc)`` – popisy pro potvrzení/report („žádný“,
    „2d 3h“). Počítá přes stávající ``cooldown_remaining``.
    """
    cooldowns = tx.get("cooldowns.json", {})
    ht3 = tx.get("ht3_cooldowns.json", {})

    if action == "clear_queue":
        old = format_duration(
            cooldown_remaining(cooldowns, player_id, now, queue_cooldown_ms)
        )
        cooldowns.pop(player_id, None)
        tx.set("cooldowns.json", cooldowns)
        return f"waitlist: {old}", "waitlist: žádný"

    if action == "set_queue":
        old = format_duration(
            cooldown_remaining(cooldowns, player_id, now, queue_cooldown_ms)
        )
        cooldowns[player_id] = now
        tx.set("cooldowns.json", cooldowns)
        return (
            f"waitlist: {old}",
            f"waitlist: {format_duration(queue_cooldown_ms)} (od teď)",
        )

    kit_key = (kit_key or "").strip().lower()
    if not kit_key:
        raise ValueError("Pro HT3+ cooldown je potřeba kit.")
    bucket = ht3.setdefault(player_id, {})
    old_ht3 = format_duration(
        ht3_cooldown_remaining(ht3, player_id, kit_key, now)
    )
    if action == "clear_ht3":
        bucket.pop(kit_key, None)
        tx.set("ht3_cooldowns.json", ht3)
        return f"HT3 {kit_key}: {old_ht3}", f"HT3 {kit_key}: žádný"

    if action == "set_ht3":
        bucket[kit_key] = now + ht3_cooldown_ms
        tx.set("ht3_cooldowns.json", ht3)
        return (
            f"HT3 {kit_key}: {old_ht3}",
            f"HT3 {kit_key}: {format_duration(ht3_cooldown_ms)} (od teď)",
        )

    raise ValueError(f"Neznámá akce cooldownu: {action}")


# ---------------------------------------------------------------------------
# Migrace klíčů identity (při změně Discord ID / IGN)
# ---------------------------------------------------------------------------
def _migrate_identity_keys(tx, old_id: str, new_id: str) -> list:
    """Přemapuje per-hráč klíče na nové Discord ID (stejný hráč, žádná ztráta).

    - cooldowns.json / ht3_cooldowns.json – klíčované Discord ID,
    - ht_tickets.json – ``ownerId``,
    - ht_results.json – ``playerId`` (výsledky zůstávají hráči),
    - queue.json / pulled_players.json – ``id`` / klíč záznamu.

    Vrací názvy změněných souborů (idempotentní: nic = prázdný seznam).
    """
    if old_id == new_id:
        return []
    changed = []

    cooldowns = tx.get("cooldowns.json", {})
    if isinstance(cooldowns, dict) and old_id in cooldowns:
        cooldowns[new_id] = cooldowns.pop(old_id)
        tx.set("cooldowns.json", cooldowns)
        changed.append("cooldowns.json")

    ht3_cd = tx.get("ht3_cooldowns.json", {})
    if isinstance(ht3_cd, dict) and old_id in ht3_cd:
        bucket = ht3_cd.pop(old_id)
        ht3_cd.setdefault(new_id, {}).update(bucket if isinstance(bucket, dict) else {})
        tx.set("ht3_cooldowns.json", ht3_cd)
        changed.append("ht3_cooldowns.json")

    tickets = tx.get(HT_TICKETS_FILE, {})
    touched = False
    for t in tickets.values():
        if isinstance(t, dict) and str(t.get("ownerId") or "") == old_id:
            t["ownerId"] = new_id
            touched = True
    if touched:
        tx.set(HT_TICKETS_FILE, tickets)
        changed.append(HT_TICKETS_FILE)

    results = tx.get(HT_RESULTS_FILE, {})
    touched = False
    for r in results.values():
        if isinstance(r, dict) and str(r.get("playerId") or "") == old_id:
            r["playerId"] = new_id
            touched = True
    if touched:
        tx.set(HT_RESULTS_FILE, results)
        changed.append(HT_RESULTS_FILE)

    queue = tx.get("queue.json", [])
    touched = False
    for entry in queue:
        if isinstance(entry, dict) and str(entry.get("id") or "") == old_id:
            entry["id"] = new_id
            touched = True
    if touched:
        tx.set("queue.json", queue)
        changed.append("queue.json")

    pulled = tx.get("pulled_players.json", {})
    if isinstance(pulled, dict) and old_id in pulled:
        pulled[new_id] = pulled.pop(old_id)
        tx.set("pulled_players.json", pulled)
        changed.append("pulled_players.json")

    return changed


def _migrate_eval_ign(tx, old_ign: str, new_ign: str) -> bool:
    """Přemapuje eval statusy (evals.json) na nové IGN. Vrací, zda se změnilo."""
    old_key = (old_ign or "").strip().lower()
    new_key = (new_ign or "").strip().lower()
    if not old_key or old_key == new_key:
        return False
    evals = tx.get("evals.json", {})
    touched = False
    for kit_key, bucket in (evals.items() if isinstance(evals, dict) else []):
        if not isinstance(bucket, dict) or old_key not in bucket:
            continue
        bucket[new_key] = bucket.pop(old_key)
        touched = True
    if touched:
        tx.set("evals.json", evals)
    return touched


# ---------------------------------------------------------------------------
# Aplikace jedné potvrzené změny (transakce + audit atomicky)
# ---------------------------------------------------------------------------
def _files_for_edit(edit: dict) -> tuple:
    field = edit.get("field")
    if field == "discord_id":
        return (
            "players.json",
            "cooldowns.json",
            "ht3_cooldowns.json",
            HT_TICKETS_FILE,
            HT_RESULTS_FILE,
            "queue.json",
            "pulled_players.json",
            EDITUSER_LOG_FILE,
        )
    if field == "ign":
        return ("players.json", "evals.json", EDITUSER_LOG_FILE)
    if field == "tier":
        return ("players.json", EDITUSER_LOG_FILE)
    if field == "cooldown":
        return ("cooldowns.json", "ht3_cooldowns.json", EDITUSER_LOG_FILE)
    raise ValueError(f"Neznámé pole úpravy: {field}")


async def apply_player_edit(
    *,
    player_id: str,
    edit: dict,
    actor_id=None,
    actor_name="",
    now: int = None,
    queue_cooldown_ms: int = 0,
    ht3_cooldown_ms: int = 0,
) -> dict:
    """Aplikuje JEDNU potvrzenou změnu hráče ATOMICky + audit ve stejné transakci.

    Tvary ``edit``:
      {"field": "discord_id", "new_value": "123456789012345678"}
      {"field": "ign", "new_value": "NewIgn"}
      {"field": "tier", "kit": "molepvp", "tier": "HT2", "retired": False}
      {"field": "cooldown", "action": "clear_queue" | "set_queue" |
                                  "clear_ht3" | "set_ht3", "kit": "molepvp"}

    Vrací dict::
      {
        "status": "changed" | "unchanged" | "error" | "not_found",
        "field"/"kit"/"old_value"/"new_value",
        "message", "audit" (záznam nebo None), "changed_files": [...],
      }

    Konflikty identity / neplatné tiery / poškozený JSON → ``status="error"``
    (nic se neuloží, soubor zůstává nedotčený).
    """
    if now is None:
        now = _now_ms()
    field = edit.get("field")
    files = _files_for_edit(edit)

    async def _run(tx):
        info = {"old_value": None, "new_value": None, "kit": ""}

        if field == "cooldown":
            try:
                old_desc, new_desc = apply_cooldown_edit(
                    tx,
                    player_id=str(player_id),
                    action=edit.get("action"),
                    kit_key=edit.get("kit"),
                    now=now,
                    queue_cooldown_ms=queue_cooldown_ms,
                    ht3_cooldown_ms=ht3_cooldown_ms,
                )
            except ValueError as exc:
                return {"status": "error", "message": str(exc), "audit": None}
            info["old_value"], info["new_value"] = old_desc, new_desc
            info["kit"] = str(edit.get("kit") or "").strip().lower()
            if old_desc == new_desc:
                return {
                    "status": OUTCOME_UNCHANGED,
                    "old_value": old_desc,
                    "new_value": new_desc,
                    "audit": None,
                }
            return await _commit(tx, info, changed_files=["cooldowns.json", "ht3_cooldowns.json"])

        players = tx.get("players.json", [])
        if not isinstance(players, list):
            players = []
        ref = {"discordId": player_id}
        if _locate(players, ref) is None:
            # players.json je dict (poškozený) → transakce už selhala výše (strict)
            return {"status": "not_found", "message": f"Hráč `<@{player_id}>` nemá záznam v players.json.", "audit": None}

        if field == "discord_id":
            old_id = str(player_id)
            new_id = str(edit.get("new_value") or "").strip()
            before = _locate(players, ref)
            old_val = str((before or {}).get("discordId") or "")
            try:
                new_players, target, outcome = change_player_discord(players, ref, new_id)
            except PlayerIdentityConflict as exc:
                return {"status": "error", "message": str(exc), "audit": None}
            if outcome == CLAIM_UNCHANGED:
                return {"status": OUTCOME_UNCHANGED, "old_value": old_val, "new_value": old_val, "audit": None}
            tx.set("players.json", new_players)
            migrated = _migrate_identity_keys(tx, old_id, new_id)
            info["old_value"], info["new_value"] = old_val, new_id
            return await _commit(tx, info, changed_files=["players.json"] + migrated)

        if field == "ign":
            before = _locate(players, ref)
            old_ign = str((before or {}).get("username") or "")
            new_ign = str(edit.get("new_value") or "").strip()
            try:
                new_players, target, outcome = change_player_ign(players, ref, new_ign)
            except PlayerIdentityConflict as exc:
                return {"status": "error", "message": str(exc), "audit": None}
            if outcome == CLAIM_UNCHANGED and str((target or {}).get("username") or "") == old_ign:
                return {"status": OUTCOME_UNCHANGED, "old_value": old_ign, "new_value": old_ign, "audit": None}
            tx.set("players.json", new_players)
            changed_files = ["players.json"]
            if _migrate_eval_ign(tx, old_ign, str((target or {}).get("username") or "")):
                changed_files.append("evals.json")
            info["old_value"], info["new_value"] = old_ign, str((target or {}).get("username") or "")
            return await _commit(tx, info, changed_files=changed_files)

        if field == "tier":
            kit_key = str(edit.get("kit") or "").strip().lower()
            try:
                new_players, target, old_val, outcome = change_kit_tier(
                    players,
                    ref,
                    kit_key,
                    edit.get("tier"),
                    retired=bool(edit.get("retired")),
                )
            except InvalidTierEdit as exc:
                return {"status": "error", "message": str(exc), "audit": None}
            if outcome == OUTCOME_UNCHANGED:
                return {"status": OUTCOME_UNCHANGED, "old_value": old_val, "new_value": old_val, "kit": kit_key, "audit": None}
            tx.set("players.json", new_players)
            info["old_value"], info["new_value"] = old_val, _mode_tier(
                (target or {}).get("modes") or {}, kit_key
            )
            info["kit"] = kit_key
            return await _commit(tx, info, changed_files=["players.json"])

        return {"status": "error", "message": f"Neznámé pole úpravy: {field}", "audit": None}

    async def _commit(tx, info, changed_files):
        entry = {
            "ts": now,
            "actorId": str(actor_id) if actor_id is not None else None,
            "actorName": actor_name or "",
            "playerId": str(player_id),
            "field": field,
            "kit": info.get("kit") or None,
            "oldValue": info.get("old_value"),
            "newValue": info.get("new_value"),
        }
        entries = tx.get(EDITUSER_LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        tx.set(EDITUSER_LOG_FILE, entries)
        return {
            "status": OUTCOME_CHANGED,
            "field": field,
            "kit": info.get("kit"),
            "old_value": info.get("old_value"),
            "new_value": info.get("new_value"),
            "message": f"Změna pole `{field}` aplikována.",
            "audit": entry,
            "changed_files": changed_files,
        }

    try:
        return await transaction(tuple(files), _run)
    except DataCorruptionError as exc:
        return {"status": "error", "message": f"Poškozená data: {exc}", "audit": None}


# ---------------------------------------------------------------------------
# Orchestrace celé potvrzené úpravy: DB → Discord role → web (best effort)
# ---------------------------------------------------------------------------
async def execute_player_edit(
    *,
    player_id: str,
    edit: dict,
    actor_id=None,
    actor_name="",
    now: int = None,
    queue_cooldown_ms: int = 0,
    ht3_cooldown_ms: int = 0,
    player_after=None,
    role_context=None,
    apply_roles=None,
    push_web=None,
    web_message: str = "edituser: aktualizace hráče na web",
) -> dict:
    """Provede potvrzenou úpravu a vrátí report SUCCESS / PARTIAL SUCCESS / FAILURE.

    Kanonická změna se NIKDY neztratí kvůli selhání role/web synchronizace:
      - 1) DB (``apply_player_edit``, transakce + audit) – jediný zdroj pravdy,
      - 2) Discord role (best effort; selhání → PARTIAL SUCCESS),
      - 3) web/GitHub přes existující ``sync_website`` (best effort; selhání →
         PARTIAL SUCCESS).

    ``apply_roles(plan) -> [ {op, roleId, ok, error}, ... ]`` – Discord vrstva
    (cog) provede akce; ``push_web(canonical) -> {"ok": bool, "message": str}``
    – cog deleguje na services/websync.sync_website. Obojí je volitelné
    (bez vrstvy se synchronizace přeskočí s poznámkou).
    """
    if now is None:
        now = _now_ms()

    db = await apply_player_edit(
        player_id=player_id,
        edit=edit,
        actor_id=actor_id,
        actor_name=actor_name,
        now=now,
        queue_cooldown_ms=queue_cooldown_ms,
        ht3_cooldown_ms=ht3_cooldown_ms,
    )
    if db["status"] in ("error", "not_found"):
        return _report(STATUS_FAILURE, db=db, roles=None, web=None)

    roles_result = {"skipped": True, "note": "", "actions": [], "errors": []}
    web_result = {"skipped": True, "note": "", "ok": None, "errors": []}
    if db["status"] != OUTCOME_CHANGED:
        return _report(
            STATUS_SUCCESS,
            db=db,
            roles=roles_result,
            web=web_result,
            message="Beze změny (idempotentní opakování stejné úpravy).",
        )

    # --- Discord role (jen změna AKTUÁLNÍHO tieru) ---
    if db["field"] == "tier" and role_context is not None and apply_roles is not None:
        kit_key = db.get("kit") or ""
        if player_after is None:
            # Kanonickou podobu hráče PO změně doplníme ze souboru (právě
            # zapsáno výše) – stejně jako když ji dodá volající.
            from services.player_identity import find_by_discord_id
            from services.store import read as _store_read

            current = await _store_read("players.json", [])
            player_after = find_by_discord_id(
                current if isinstance(current, list) else [], str(player_id)
            )
        plan = plan_role_sync(
            player=player_after or {},
            member=role_context.get("member"),
            roles_map=role_context.get("roles_map") or {},
            kit_key=kit_key,
            kit_display=role_context.get("kit_display") or {},
        )
        roles_result["skipped"] = not plan["synced"]
        roles_result["note"] = plan["note"]
        if plan["synced"]:
            try:
                applied = await apply_roles(plan["actions"])
            except Exception as exc:  # noqa: BLE001 – role nesmí shodit report
                log.exception("Aplikace rolí (edituser) selhala: %s", exc)
                roles_result["errors"] = [f"aplikace rolí selhala: {exc}"]
                applied = []
            roles_result["actions"] = list(applied or [])
            roles_result["errors"] += [
                a.get("error") for a in (applied or []) if not a.get("ok")
            ]

    # --- Web (jen změny publikované v players.json) ---
    if db["field"] in ("tier", "ign", "discord_id") and push_web is not None:
        from services.store import read as _store_read

        canonical = await _store_read("players.json", [])
        if isinstance(canonical, list) and canonical:
            try:
                wresult = await push_web(canonical)
            except Exception as exc:  # noqa: BLE001 – web nesmí shodit report
                log.exception("Push na web (edituser) selhal: %s", exc)
                wresult = {"ok": False, "message": f"web sync selhal: {exc}"}
            web_result["skipped"] = False
            web_result["ok"] = bool(wresult.get("ok")) if isinstance(wresult, dict) else False
            if not web_result["ok"]:
                msg = (
                    wresult.get("message") if isinstance(wresult, dict) else str(wresult)
                )
                web_result["errors"] = [msg] if msg else ["web sync selhal"]

    errors = roles_result["errors"] + web_result["errors"]
    status = STATUS_PARTIAL if errors else STATUS_SUCCESS
    summary = []
    if roles_result["actions"]:
        ok_n = sum(1 for a in roles_result["actions"] if a.get("ok"))
        summary.append(f"Role: {ok_n}/{len(roles_result['actions'])}")
    if not web_result["skipped"]:
        summary.append(f"Web: {'OK' if web_result['ok'] else 'SELHAL'}")
    message = ", ".join(filter(None, summary)) or "DB změna aplikována."
    return _report(status, db=db, roles=roles_result, web=web_result, message=message)


def _report(status, *, db, roles, web, message=""):
    return {
        "status": status,
        "db": db,
        "roles": roles,
        "web": web,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Auditní log (data/edituser_log.json, append-only, restart-safe)
# ---------------------------------------------------------------------------
async def get_edituser_log() -> list:
    """Všechny záznamy auditu editoru v pořadí zápisu."""
    entries = await store_read(EDITUSER_LOG_FILE, [])
    return [e for e in entries if isinstance(e, dict)]
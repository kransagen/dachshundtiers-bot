"""Synchronizace webu (players.json na GitHubu) s kanonickou databází.

Jak web spotřebovává data (ověřeno – viz ``github_sync.py``)
-------------------------------------------------------------
Web (DachshundTiers) čte ``players.json`` na GitHubu (repo + cesta
z ``GITHUB_*`` env). Jeho jediným zapisovatelem je tento bot:
- ``/result`` a ``/checkweb`` posílají změny přes ``github_sync.push_players``
  (PUTo přes GitHub Contents API, při konfliktu 409 se merge zopakuje na
  čerstvých datech, všechny pushy sdílí jeden lock),
- bez ``GITHUB_TOKEN`` se web nikdy nemění.

Neexistuje žádná druhá databáze: pokaždé se vybere **kanonická** hráčská
databáze (``data/players.json``) a ta se po potvrzení zapíše na web.

Požadavky Phase 5
-----------------
- ``/websync preview`` – jen porovná web s kanonickou DB (detekce),
- ``/websync apply``   – po **explicitním potvrzení** nahradí web kanonickou DB,
- detekce: chybějící hráči na webu, špatné tiery, zastaralá data, duplicitní
  hráči, neplatné záznamy,
- po synchronizaci se zapíše do ``data/websync_log.json`` (audit):
  timestamp, počet záznamů, úspěch/selhání a chyby,
- retry handling: čtení webu i zápis se opakují s prodlevou (transientní
  selhání; konflikt 409 už zvládá ``github_sync.push_players`` interně).

Ochrany
-------
- prázdná kanonická DB se na web NIKDY neposílá (smazala by ho),
- bez ``GITHUB_TOKEN`` se web nečte ani nezapisuje,
- poškozené kanonické záznamy (ne-objekt / bez username) se vynechají –
  na webu by jinak vznikly neplatné záznamy.

Žádná závislost na discord.py → snadné testy.
"""

import asyncio
import copy
import hashlib
import json
import logging
import time

import github_sync
from services.store import read as store_read, transaction

log = logging.getLogger("dachshundtiers")

WEBSYNC_LOG_FILE = "websync_log.json"

DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_S = 1.0

# Kategorie detekcí (názvy podle požadavku)
KINDS = (
    "missing_player",   # hráč v kanonické DB, na webu chybí
    "wrong_tier",       # web má pro hráče+kit jiný tier než kanonická DB
    "stale_data",       # historie na webu je starší (kratší) než kanonická
    "duplicate_player", # stejný username na webu vícekrát
    "invalid_record",   # neplatný záznam na webu (struktura)
)

KIND_LABELS = {
    "missing_player": "🚫 Chybějící hráči na webu",
    "wrong_tier": "✏️ Špatné tiery",
    "stale_data": "🗓️ Zastaralá data",
    "duplicate_player": "🔁 Duplicitní hráči",
    "invalid_record": "❌ Neplatné záznamy",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_player(p: dict) -> str:
    return str(p.get("username", "") or "").strip()


def _clean_canonical(canonical) -> list:
    """Kanonická DB bez poškozených záznamů (objekty s platným username)."""
    out = []
    for p in (canonical or []):
        if not isinstance(p, dict):
            continue
        if not _normalize_player(p):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Analýza (čistá funkce – nic nemění)
# ---------------------------------------------------------------------------
def _record_problem(entry: dict) -> str | None:
    """Vrátí popis strukturálního problému záznamu webu, jinak None."""
    if not isinstance(entry, dict):
        return "záznam není objekt"
    if not _normalize_player(entry):
        return "chybí username"
    modes = entry.get("modes")
    if modes is not None and not isinstance(modes, dict):
        return "modes není objekt"
    history = entry.get("history")
    if history is not None and not isinstance(history, dict):
        return "history není objekt"
    for kit, tier in (modes or {}).items():
        if not isinstance(tier, str) or not str(tier).strip():
            return f"tier pro {kit} je prázdný/neplatný"
    return None


def analyze_websync(canonical, website) -> dict:
    """Porovná web s kanonickou DB; vrátí nálezy + souhrn (nic neaplikuje).

    Vrací::
        {
          "summary":        {kind: počet},  # všechny kategorie, i 0
          "findings":       [nález, ...],
          "canonical_count": počet záznamů kanonické DB (čistá),
          "website_count":   počet unikátních username na webu,
          "website_only":    unikátní hráči webu, kteří v kanonické DB nejsou,
          "has_issues":      bool(findings),
        }
    """
    canonical = _clean_canonical(canonical)
    if website is None:
        website = []
    elif not isinstance(website, list):
        website = list(website)  # přijmeme i tuple/iterátory (testy/volající)
    raw_website = website[:]

    summary = {kind: 0 for kind in KINDS}
    findings: list = []

    # 1) Struktura + index webu (duplicity a neplatné záznamy)
    index: dict[str, list] = {}
    for entry in raw_website:
        problem = _record_problem(entry)
        if problem is not None:
            username_marker = (
                entry.get("username", "") if isinstance(entry, dict) else ""
            )
            findings.append(
                {
                    "kind": "invalid_record",
                    "username": str(username_marker or "").strip(),
                    "kit": "",
                    "canonical_tier": None,
                    "website_tier": None,
                    "reason": problem,
                    "message": (
                        f"❌ Záznam na webu není platný ({problem}) – při "
                        "synchronizaci se odebere."
                    ),
                }
            )
            summary["invalid_record"] += 1
            continue
        key = _normalize_player(entry).lower()
        index.setdefault(key, []).append(entry)

    for key, entries in sorted(index.items()):
        if len(entries) > 1:
            username = _normalize_player(entries[0])
            findings.append(
                {
                    "kind": "duplicate_player",
                    "username": username,
                    "kit": "",
                    "canonical_tier": None,
                    "website_tier": None,
                    "reason": f"{len(entries)}×",
                    "message": (
                        f"🔁 **{username}** je na webu **{len(entries)}×** – "
                        "duplicity se při synchronizaci sloučí."
                    ),
                }
            )
            summary["duplicate_player"] += 1

    # 2) Kanonická DB vs web
    canonical_keys = set()
    for p in canonical:
        username = _normalize_player(p)
        key = username.lower()
        canonical_keys.add(key)
        entries_web = index.get(key)
        if not entries_web:
            findings.append(
                {
                    "kind": "missing_player",
                    "username": username,
                    "kit": "",
                    "canonical_tier": None,
                    "website_tier": None,
                    "reason": "",
                    "message": (
                        f"🚫 **{username}** je v kanonické DB, ale na webu "
                        "chybí – při synchronizaci se přidá."
                    ),
                }
            )
            summary["missing_player"] += 1
            continue

        w = entries_web[0]
        cmodes = p.get("modes") if isinstance(p.get("modes"), dict) else {}
        wmodes = w.get("modes") if isinstance(w.get("modes"), dict) else {}

        # Špatné tiery (kanonická hodnota ≠ web; webový může i chybět)
        for kit in sorted(cmodes):
            canonical_tier = str(cmodes[kit] or "").strip().upper()
            if not canonical_tier:
                continue
            raw_web = wmodes.get(kit)
            web_tier = str(raw_web or "").strip().upper() if raw_web else None
            if web_tier == canonical_tier:
                continue
            findings.append(
                {
                    "kind": "wrong_tier",
                    "username": username,
                    "kit": kit,
                    "canonical_tier": canonical_tier,
                    "website_tier": web_tier,
                    "reason": "",
                    "message": (
                        f"✏️ **{username}** · **{kit}**: web má "
                        f"{web_tier or 'žádný tier'}, kanonická DB má "
                        f"**{canonical_tier}**."
                    ),
                }
            )
            summary["wrong_tier"] += 1

        # Zastaralá data: historie na webu je kratší než kanonická
        chist = p.get("history") if isinstance(p.get("history"), dict) else {}
        whist = w.get("history") if isinstance(w.get("history"), dict) else {}
        stale_kits = []
        for kit in sorted(set(chist) | set(whist)):
            c_len = len([h for h in (chist.get(kit) or []) if isinstance(h, dict)])
            w_len = len([h for h in (whist.get(kit) or []) if isinstance(h, dict)])
            if c_len and w_len < c_len:
                stale_kits.append(f"{kit} ({w_len} z {c_len})")
        if stale_kits:
            findings.append(
                {
                    "kind": "stale_data",
                    "username": username,
                    "kit": "",
                    "canonical_tier": None,
                    "website_tier": None,
                    "reason": ", ".join(stale_kits),
                    "message": (
                        f"🗓️ **{username}** – historie na webu je starší "
                        f"({', '.join(stale_kits)}) – při synchronizaci se doplní."
                    ),
                }
            )
            summary["stale_data"] += 1

    website_only = [k for k in sorted(index) if k not in canonical_keys]
    return {
        "summary": summary,
        "findings": findings,
        "canonical_count": len(canonical),
        "website_count": len(index),
        "website_only": website_only,
        "has_issues": bool(findings),
    }


def fingerprint_canonical(canonical) -> str:
    """Deterministický otisk kanonické DB – porovnání preview vs potvrzení."""
    clean = sorted(
        (json.dumps(p, ensure_ascii=False, sort_keys=True) for p in _clean_canonical(canonical))
    )
    return hashlib.sha1("\n".join(clean).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Čtení webu s retry + zápis s retry
# ---------------------------------------------------------------------------
async def _fetch_with_retries(attempts: int, retry_delay_s: float, errors: list):
    """Stáhne web; transientní selhání opakuje. Vrací (players, sha, used_attempts).

    Bez tokenu (``(None, None, None)``) nepokračuje – retry by nepomohl.
    """
    for attempt in range(1, attempts + 1):
        players, sha, error = await github_sync.fetch_players()
        if players is not None:
            return players, sha, attempt
        if error is None:  # bez GITHUB_TOKEN
            return None, None, attempt
        errors.append(error)
        if attempt < attempts:
            await asyncio.sleep(retry_delay_s)
    return None, None, attempts


def _is_no_token_error(msg: str) -> bool:
    return "GITHUB_TOKEN" in (msg or "")


# ---------------------------------------------------------------------------
# Preview / synchronizace
# ---------------------------------------------------------------------------
def _result(
    *,
    ok,
    message,
    records,
    ts,
    analysis=None,
    errors=None,
    attempts=0,
    fingerprint="",
) -> dict:
    return {
        "ok": ok,
        "message": message,
        "records": records,
        "ts": ts,
        "analysis": analysis,
        "errors": list(errors or []),
        "attempts": attempts,
        "fingerprint": fingerprint,
    }


async def preview_website(
    *,
    canonical,
    attempts: int = None,
    retry_delay_s: float = None,
    log_event: bool = True,
    actor_id=None,
    actor_name="",
    now: int = None,
) -> dict:
    """Stáhne web a porovná ho s kanonickou DB (nic nezapisuje)."""
    attempts = attempts or DEFAULT_ATTEMPTS
    if retry_delay_s is None:
        retry_delay_s = DEFAULT_RETRY_DELAY_S
    if now is None:
        now = _now_ms()
    canonical = _clean_canonical(canonical)

    errors: list = []
    website, _, used = await _fetch_with_retries(attempts, retry_delay_s, errors)
    if website is None:
        message = (
            "⚠️ GITHUB_TOKEN není nastaven – web se nedá číst ani zapsat."
            if not errors
            else f"❌ Web se nepodařilo přečíst: {errors[-1]}"
        )
        result = _result(
            ok=False, message=message, records=len(canonical), ts=now,
            analysis=None, errors=errors, attempts=used,
        )
        if log_event:
            await log_websync_event(
                actor_id=actor_id, actor_name=actor_name, mode="preview",
                status="failure", records=len(canonical),
                errors=errors, attempts=used, ts=now,
            )
        return result

    analysis = analyze_websync(canonical, website)
    result = _result(
        ok=True, message="",
        records=len(canonical), ts=now, analysis=analysis,
        errors=errors, attempts=used,
        fingerprint=fingerprint_canonical(canonical),
    )
    if log_event:
        await log_websync_event(
            actor_id=actor_id, actor_name=actor_name, mode="preview",
            status="success", records=len(canonical),
            website_count=analysis["website_count"],
            findings=analysis["summary"], errors=errors,
            attempts=used, ts=now,
        )
    return result


async def sync_website(
    *,
    canonical,
    message: str,
    attempts: int = None,
    retry_delay_s: float = None,
    log_event: bool = True,
    actor_id=None,
    actor_name="",
    now: int = None,
) -> dict:
    """Po potvrzení nahradí players.json na webu kanonickou DB (s retry).

    - prázdnou kanonickou DB nikdy neposílá (smazala by web),
    - bez ``GITHUB_TOKEN`` se nepokouší zápisem,
    - čtení webu i zápis se při transientním selhání opakují,
    - po pokusu zapíše do ``data/websync_log.json`` (timestamp, počet záznamů,
      úspěch/selhání a chyby).
    """
    attempts = attempts or DEFAULT_ATTEMPTS
    if retry_delay_s is None:
        retry_delay_s = DEFAULT_RETRY_DELAY_S
    if now is None:
        now = _now_ms()
    canonical = _clean_canonical(canonical)

    # Bezpečnostní zámka: prázdná DB by web vymazala.
    if not canonical:
        result = _result(
            ok=False,
            message="❌ Kanonická players.json je prázdná – web se NEpřepisuje.",
            records=0, ts=now, analysis=None, attempts=0,
        )
        if log_event:
            await log_websync_event(
                actor_id=actor_id, actor_name=actor_name, mode="apply",
                status="failure", records=0, errors=["kanonická DB je prázdná"],
                attempts=0, ts=now,
            )
        return result

    # 1) Čtení webu (s retry) – pro analýzu, kterou víme, co synchronizace udělá.
    errors: list = []
    website, _, used = await _fetch_with_retries(attempts, retry_delay_s, errors)
    if website is None:
        message = (
            "⚠️ GITHUB_TOKEN není nastaven – web se nedá číst ani zapsat."
            if not errors
            else f"❌ Web se nepodařilo přečíst: {errors[-1]}"
        )
        result = _result(
            ok=False, message=message, records=len(canonical), ts=now,
            analysis=None, errors=errors, attempts=used,
        )
        if log_event:
            await log_websync_event(
                actor_id=actor_id, actor_name=actor_name, mode="apply",
                status="failure", records=len(canonical),
                website_count=None, errors=errors, attempts=used, ts=now,
            )
        return result

    analysis = analyze_websync(canonical, website)

    # 2) Zápis kanonické DB na web (409 zvládá github_sync interně).
    def _replace(_players):
        return copy.deepcopy(canonical)

    push_attempts = 0
    for attempt in range(1, attempts + 1):
        push_attempts = attempt
        ok, msg, _last_built = await github_sync.push_players(
            message,
            _replace,
            success_message=(
                "✅ players.json na webu nahrazen kanonickou databází "
                "– web je aktuální."
            ),
        )
        if ok:
            result = _result(
                ok=True, message=msg, records=len(canonical), ts=now,
                analysis=analysis, errors=errors, attempts=used + push_attempts,
                fingerprint=fingerprint_canonical(canonical),
            )
            if log_event:
                await log_websync_event(
                    actor_id=actor_id, actor_name=actor_name, mode="apply",
                    status="success", records=len(canonical),
                    website_count=analysis["website_count"],
                    findings=analysis["summary"], errors=errors,
                    attempts=used + push_attempts, ts=now,
                )
            return result
        errors.append(msg)
        if _is_no_token_error(msg) or attempt >= attempts:
            break
        await asyncio.sleep(retry_delay_s)

    result = _result(
        ok=False, message=errors[-1] if errors else "❌ Zápis na web selhal.",
        records=len(canonical), ts=now, analysis=analysis,
        errors=errors, attempts=used + push_attempts,
    )
    if log_event:
        await log_websync_event(
            actor_id=actor_id, actor_name=actor_name, mode="apply",
            status="failure", records=len(canonical),
            website_count=analysis["website_count"],
            findings=analysis["summary"], errors=errors,
            attempts=used + push_attempts, ts=now,
        )
    return result


# ---------------------------------------------------------------------------
# Auditní log (data/websync_log.json, append-only, restart-safe)
# ---------------------------------------------------------------------------
async def log_websync_event(
    *,
    actor_id=None,
    actor_name="",
    mode: str,
    status: str,
    records: int,
    website_count: int = None,
    findings: dict = None,
    errors: list = None,
    attempts: int = 0,
    ts: int = None,
) -> dict:
    """Přidá záznam o synchronizaci do auditu (atomicky, append-only)."""
    if ts is None:
        ts = _now_ms()
    entry: dict = {
        "ts": ts,
        "mode": mode,
        "status": status,
        "records": int(records),
        "websiteCount": website_count,
        "findings": dict(findings or {}),
        "errors": list(errors or []),
        "attempts": int(attempts),
    }
    if actor_id is not None:
        entry["actorId"] = str(actor_id)
        entry["actorName"] = actor_name or ""

    async def _run(tx):
        entries = tx.get(WEBSYNC_LOG_FILE, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        tx.set(WEBSYNC_LOG_FILE, entries)
        return entry

    return await transaction((WEBSYNC_LOG_FILE,), _run)


async def get_websync_log() -> list:
    """Všechny záznamy auditu v pořadí zápisu (chronologicky)."""
    entries = await store_read(WEBSYNC_LOG_FILE, [])
    return [e for e in entries if isinstance(e, dict)]
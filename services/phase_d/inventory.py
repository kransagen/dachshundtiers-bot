"""Phase D, D1 — migration inventory + report generator (pure, no DB).

Walks the JSON data directory and produces the statistics the Phase D report
will cite: record counts, identity problems (missing/blank/duplicate IGNs),
date sanity, tier-code census, and the stores that are *expected* to stay
unresolvable until identities are claimed (discord-keyed stores cannot map to
the relational ``players`` table, which starts empty — players.json carries no
Discord IDs).

The inventory is intentionally DB-free: tier-code *validation* against the
relational catalogue happens in the D2 importer, where the session exists.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATE_DD_MM_YYYY = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

UNRESOLVED_REASON = (
    "players.json nese žádná Discord ID a relační players tabulka startuje prázdná; "
    "klíče zůstávají unresolved (nepřiřazené), dokud neproběhne /linkdiscord."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_json(path: Path):
    """Raw parse; raises ``json.JSONDecodeError``/``UnicodeDecodeError``."""
    return json.loads(path.read_text(encoding="utf-8"))


def inventory_players(records: list) -> dict:
    """Per-player census: identity problems, kit/tier census, date sanity."""
    total = len(records)
    no_username: list[str] = []
    empty_ign: list[str] = []
    duplicate_igns: dict[str, list[int]] = {}
    with_discord_id: list[str] = []

    kits: set[str] = set()
    modes_codes: dict[str, int] = {}
    history_entries = 0
    history_by_kit: dict[str, int] = {}
    tier_codes: set[str] = set()
    invalid_dates: list[dict] = []
    non_dict: list[int] = []

    for idx, record in enumerate(records):
        if not isinstance(record, dict):
            non_dict.append(idx)
            continue
        username = record.get("username")
        if username is None:
            no_username.append(str(idx))
        elif str(username).strip() == "":
            empty_ign.append(str(idx))
        else:
            key = str(username).strip().lower()
            duplicate_igns.setdefault(key, []).append(idx)

        if any(record.get(k) is not None for k in ("discordId", "discord_id", "id")):
            with_discord_id.append(str(idx))

        modes = record.get("modes")
        if not isinstance(modes, dict):
            modes = {}
        for kit, code in modes.items():
            kits.add(kit)
            modes_codes[str(code)] = modes_codes.get(str(code), 0) + 1
            tier_codes.add(str(code))

        history = record.get("history")
        if not isinstance(history, dict):
            history = {}
        for kit, entries in history.items():
            kits.add(kit)
            if not isinstance(entries, list):
                continue
            history_entries += len(entries)
            history_by_kit[kit] = history_by_kit.get(kit, 0) + len(entries)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                date = entry.get("date")
                code = entry.get("tier")
                if code is not None:
                    tier_codes.add(str(code))
                if date is not None and not DATE_DD_MM_YYYY.match(str(date).strip()):
                    invalid_dates.append(
                        {"player_idx": idx, "username": username, "kit": kit, "date": date}
                    )

    return {
        "total": total,
        "non_dict_records": non_dict,
        "with_discord_id": with_discord_id,
        "no_username": no_username,
        "empty_ign": empty_ign,
        "duplicate_igns": {
            ign: idxs for ign, idxs in sorted(duplicate_igns.items()) if len(idxs) > 1
        },
        "kits": sorted(kits),
        "kit_count": len(kits),
        "modes_entries": sum(modes_codes.values()),
        "modes_tier_codes": dict(sorted(modes_codes.items())),
        "history_entries": history_entries,
        "history_by_kit": dict(sorted(history_by_kit.items())),
        "tier_codes": sorted(tier_codes),
        "invalid_dates": invalid_dates,
    }


def inventory_data_dir(data_dir: Optional[Path] = None) -> dict:
    """Full inventory report for one data directory (pure, DB-free)."""
    data = Path(data_dir or __import__("storage").DATA_DIR).resolve()
    if not data.is_dir():
        raise FileNotFoundError(f"Data adresář neexistuje: {data}")

    report: dict = {
        "generated_at_utc": _utcnow().isoformat(),
        "data_dir": str(data),
        "files_present": sorted(p.name for p in data.glob("*.json")),
    }

    players_path = data / "players.json"
    if players_path.exists():
        try:
            players = _parse_json(players_path)
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            report["players"] = {"readable": False, "error": str(err)[:500]}
        else:
            if isinstance(players, list):
                report["players"] = inventory_players(players)
            else:
                report["players"] = {"readable": True, "unexpected_shape": type(players).__name__}
    else:
        report["players"] = None

    cooldowns_path = data / "cooldowns.json"
    if cooldowns_path.exists():
        raw = _parse_json(cooldowns_path)
        keys = sorted(raw.keys()) if isinstance(raw, dict) else []
        report["cooldowns_waitlist"] = {
            "records": len(keys),
            "discord_id_keys": keys,
            "all_unresolved_reason": UNRESOLVED_REASON,
        }

    ht3_path = data / "ht3_cooldowns.json"
    if ht3_path.exists():
        raw = _parse_json(ht3_path)
        if isinstance(raw, dict):
            kit_counts: dict[str, int] = {}
            for player, kits in raw.items():
                if isinstance(kits, dict):
                    for kit in kits:
                        kit_counts[kit] = kit_counts.get(kit, 0) + 1
            report["ht3_cooldowns"] = {
                "players": len(raw),
                "kit_counts": dict(sorted(kit_counts.items())),
                "all_unresolved_reason": UNRESOLVED_REASON,
            }

    kits_path = data / "kits.json"
    if kits_path.exists():
        raw = _parse_json(kits_path)
        report["kits_file"] = {"kits": raw if isinstance(raw, list) else None}

    testers_path = data / "testers.json"
    if testers_path.exists():
        raw = _parse_json(testers_path)
        report["testers"] = {
            "ids": raw if isinstance(raw, list) else None,
            "all_unresolved_reason": UNRESOLVED_REASON,
        }

    stats_path = data / "testers_stats.json"
    if stats_path.exists():
        raw = _parse_json(stats_path)
        report["testers_stats"] = {
            "players": len(raw) if isinstance(raw, dict) else None,
        }

    report["not_imported_to_relational"] = {
        "testers_stats.json": "agregované statistiky – žádný relační cíl (zůstává export-only)",
        "players.json modes": (
            "aktuální tiers z JSON NEJSOU autoritativní (Discord je jediná autorita); "
            "do mirroru ani tier_history se nekopírují"
        ),
        "queue_channels.json / panel message soubory": "runtime konfigurace, ne hráčská data",
    }
    return report


def render_markdown(report: dict) -> str:
    """Human-readable inventory report (admin + Phase D report appendix)."""
    lines = [
        f"# Phase D inventory — {report['generated_at_utc']}",
        "",
        f"- data dir: `{report['data_dir']}`",
        f"- files: {', '.join(report['files_present']) or 'žádné'}",
        "",
        "## players.json",
    ]
    players = report.get("players")
    if players is None:
        lines.append("\n*(chybí)*")
    elif players.get("readable") is False:
        lines.append(f"\n**NEČITELNÉ:** {players.get('error')}")
    elif "unexpected_shape" in players:
        lines.append(f"\n*(neočekávaný tvar: {players['unexpected_shape']})*")
    else:
        lines += [
            f"\n- total: {players['total']}",
            f"- s Discord ID: {len(players['with_discord_id'])}",
            f"- bez username: {players['no_username'] or '0'}",
            f"- prázdný IGN: {players['empty_ign'] or '0'}",
            f"- duplicitní IGN (casefold): {players['duplicate_igns'] or '0'}",
            f"- kity: {players['kit_count']} ({', '.join(players['kits'])})",
            f"- history záznamů: {players['history_entries']}",
            f"- tier kódy v datech: {', '.join(players['tier_codes']) or '—'}",
            f"- nevalidní data (DD.MM.YYYY): {len(players['invalid_dates'])}",
        ]
        if players["invalid_dates"]:
            for bad in players["invalid_dates"][:20]:
                lines.append(f"  - `{bad['username']}`/{bad['kit']}: `{bad['date']}`")

    cd = report.get("cooldowns_waitlist")
    if cd:
        lines += [
            "",
            "## cooldowns.json (waitlist)",
            f"- records: {cd['records']}",
            f"- **všechny klíče jsou Discord ID bez relačního hráče** — {cd['all_unresolved_reason']}",
        ]
    ht3 = report.get("ht3_cooldowns")
    if ht3:
        lines += [
            "",
            "## ht3_cooldowns.json",
            f"- players: {ht3['players']}",
            f"- kit counts: {ht3['kit_counts']}",
        ]
    kits = report.get("kits_file")
    if kits and kits.get("kits"):
        lines += ["", "## kits.json", f"- {len(kits['kits'])} kitů: {', '.join(kits['kits'])}"]
    testers = report.get("testers")
    if testers and testers.get("ids") is not None:
        lines += ["", "## testers.json", f"- {len(testers['ids'])} ID (Discord, zatím neresolvovaná)"]
    stats = report.get("testers_stats")
    if stats:
        lines += ["", "## testers_stats.json", f"- players: {stats['players']} (export-only)"]

    lines += ["", "## Do relačního schématu se NEimportuje", ""]
    for source, reason in (report.get("not_imported_to_relational") or {}).items():
        lines.append(f"- `{source}` — {reason}")
    return "\n".join(lines) + "\n"
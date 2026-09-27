"""D2 — idempotent relational import of the legacy JSON stores.

Authority invariants enforced here (design §16/§17, authority model):

* Discord is the sole authority for **current** tiers — ``player_current_tiers``
  is NEVER written by the importer. ``players.json`` ``modes`` is explicitly
  NOT a current-tier source and is ignored.
* ``players.json`` contributes only identity (IGN) and absolutely-dated
  history states. History rows get ``previous_tier_id = NULL``: inferring
  "previous" from file ordering would be an assumption, and the mirror's
  provenance model records transitions — not reconstructed ones.
* Discord-id-keyed stores (cooldowns / ht3_cooldowns / testers) are imported
  ONLY for players that are already linked (``players.discord_id``). Every
  unresolved key is recorded as ``MigrationImportIssue`` — never guessed,
  never auto-matched, never fuzzy-resolved.
* ``cooldowns.json`` is kit-less, but the kit IS derivable: the legacy writer
  set ``cooldowns[playerId] = now`` in the same transaction as the result
  carrying that ``now`` and its ``kit``. An unambiguous ``(playerId, timestamp)``
  match is converted to a per-kit row; anything else (no match, several kits at
  the same instant, unknown kit) is kept as an explicitly-flagged legacy global
  row with an open ``MigrationImportIssue``. Legacy rows are never deleted and
  never guessed. ``ht3_cooldowns.json`` is already per-kit and stays per-kit.
* All writes are idempotent: kits/tiers/players use get-or-create, history and
  issues dedupe on natural keys, cooldowns/testers upsert. A second run on the
  same database changes nothing.
* The whole import runs in ONE transaction (``db.services.session.transaction``)
  — any error rolls everything back.

Tier kinds: ladder codes ``HT*``/``LT*`` seed as ``kind='ladder'``; marker codes
without ladder rank semantics (e.g. ``LT3 EVAL``, ``RLT2``, ``NIC JE``) seed as
``kind='virtual'`` — imported as history facts, never as current tiers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Kit,
    MigrationImportIssue,
    Tester,
    TierHistory,
)
from db.repositories.cooldowns import COOLDOWN_HT3, COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import KitRepository, TierDefinitionRepository
from db.repositories.players import PLAYER_SOURCE_MIGRATION, PlayerRepository
from db.repositories.sync_audit import (
    AuditRepository,
    BotConfigRepository,
    MigrationIssueRepository,
)
from db.services.session import transaction

CAT_PLAYER_MALFORMED = "player_malformed_record"
CAT_PLAYER_NO_USERNAME = "player_no_username"
CAT_HISTORY_INVALID_DATE = "player_history_invalid_date"
CAT_HISTORY_UNKNOWN_KIT = "player_history_unknown_kit"
CAT_HISTORY_UNKNOWN_TIER = "player_history_unknown_tier"
CAT_COOLDOWN_UNRESOLVED = "cooldown_unresolved_player"
CAT_COOLDOWN_INVALID_KEY = "cooldown_invalid_key"
CAT_COOLDOWN_INVALID_EXPIRY = "cooldown_invalid_expiry"
CAT_COOLDOWN_KIT_UNATTRIBUTABLE = "cooldown_kit_unattributable"
CAT_COOLDOWN_KIT_UNKNOWN = "cooldown_kit_unknown_in_registry"
CAT_HT3_UNRESOLVED = "ht3_cooldown_unresolved_player"
CAT_HT3_UNKNOWN_KIT = "ht3_cooldown_unknown_kit"
CAT_HT3_INVALID_KEY = "ht3_cooldown_invalid_key"
CAT_TESTER_UNRESOLVED = "tester_unresolved_player"
CAT_TESTER_INVALID_KEY = "tester_invalid_key"

SRC_PLAYERS = "players.json"
SRC_COOLDOWNS = "cooldowns.json"
SRC_HT3 = "ht3_cooldowns.json"
SRC_TESTERS = "testers.json"
SRC_RESULTS = "ht_results.json"

HISTORY_REASON = "import z players.json (Phase D) — historický stav, nikoli aktuální tier"

BOT_CONFIG_IMPORT_KEY = "phase_d.import.latest"
AUDIT_ACTION_IMPORT = "phase_d_import"

_LADDER_CODE_RE = re.compile(r"^(?:HT|LT)\d+$")
_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _epoch_ms_to_utc(ms) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_dd_mm_yyyy(value: str) -> Optional[datetime]:
    """'DD.MM.YYYY' -> UTC midnight; None when unusable (not just invalid)."""
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value.strip())
    if match is None:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _tier_kind(code: str) -> str:
    return "ladder" if _LADDER_CODE_RE.match(code) else "virtual"


async def _history_exists(
    session: AsyncSession,
    *,
    player_id: int,
    kit_id: int,
    tier_id: int,
    changed_at: datetime,
) -> bool:
    stmt = (
        select(TierHistory.id)
        .where(
            TierHistory.player_id == player_id,
            TierHistory.kit_id == kit_id,
            TierHistory.tier_id == tier_id,
            TierHistory.changed_at == changed_at,
            TierHistory.source == "migration",
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _issue_open(
    session: AsyncSession,
    *,
    category: str,
    source_file: str,
    source_key: Optional[str],
) -> bool:
    stmt = (
        select(MigrationImportIssue.id)
        .where(
            MigrationImportIssue.status == "open",
            MigrationImportIssue.category == category,
            MigrationImportIssue.source_file == source_file,
            MigrationImportIssue.source_key == source_key,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _record_issue(
    session: AsyncSession,
    *,
    category: str,
    source_file: str,
    reason: str,
    payload: dict,
    source_key: Optional[str] = None,
) -> int:
    """Record an issue only if no identical open issue exists (idempotency)."""
    if await _issue_open(
        session, category=category, source_file=source_file, source_key=source_key
    ):
        return 0
    await MigrationIssueRepository().record(
        session,
        category=category,
        source_file=source_file,
        source_key=source_key,
        reason=reason,
        payload=payload,
    )
    return 1


async def _import_players_and_history(
    session: AsyncSession,
    *,
    report: dict,
    records: list,
    player_repo: PlayerRepository,
    kit_repo: KitRepository,
    tier_repo: TierDefinitionRepository,
) -> None:
    players_path = SRC_PLAYERS

    # Pass 1: seed tier definitions for every code in modes+history (data-driven).
    codes: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        modes = rec.get("modes")
        if isinstance(modes, dict):
            for code in modes.values():
                if code is not None:
                    codes.add(str(code))
        history = rec.get("history")
        if isinstance(history, dict):
            for entries in history.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("tier") is not None:
                        codes.add(str(entry["tier"]))
    for code in sorted(codes):
        await tier_repo.get_or_create(
            session, code=code, kind=_tier_kind(code), display_name=code
        )
        report["tiers_ensured"] += 1

    # Pass 2: players + history.
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            report["players_malformed"] += 1
            report["issues_recorded"] += await _record_issue(
                session,
                category=CAT_PLAYER_MALFORMED,
                source_file=players_path,
                source_key=f"record:{idx}",
                reason=f"Záznam #{idx} není JSON objekt.",
                payload={"record": str(rec)[:500]},
            )
            continue
        username = rec.get("username")
        if username is None or str(username).strip() == "":
            report["players_no_username"] += 1
            report["issues_recorded"] += await _record_issue(
                session,
                category=CAT_PLAYER_NO_USERNAME,
                source_file=players_path,
                source_key=f"record:{idx}",
                reason=f"Záznam #{idx} nemá nezprázdněné username.",
                payload={"record": {k: v for k, v in rec.items() if k != "history"}},
            )
            continue

        name = str(username).strip()
        before = await player_repo.get_by_ign(session, name)
        player = await player_repo.get_or_create_by_ign(
            session, ign=name, source=PLAYER_SOURCE_MIGRATION
        )
        if before is None:
            report["players_created"] += 1
        else:
            report["players_existing"] += 1

        history = rec.get("history")
        if not isinstance(history, dict):
            continue
        for kit_key, entries in history.items():
            kit = await kit_repo.get_by_key(session, kit_key)
            if kit is None:
                report["history_unknown_kit"] += 1
                report["issues_recorded"] += await _record_issue(
                    session,
                    category=CAT_HISTORY_UNKNOWN_KIT,
                    source_file=players_path,
                    source_key=f"{name}:{kit_key}",
                    reason=f"Hráč `{name}` má historii pro neznámý kit `{kit_key}`.",
                    payload={"username": name, "kit": kit_key},
                )
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                date_val = entry.get("date")
                code = entry.get("tier")
                if date_val is None or code is None:
                    continue
                changed_at = parse_dd_mm_yyyy(date_val)
                tier = await tier_repo.get_by_code(session, str(code))
                if changed_at is None:
                    report["history_invalid_dates"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_HISTORY_INVALID_DATE,
                        source_file=players_path,
                        source_key=f"{name}:{kit_key}:{date_val}",
                        reason=f"Neplatné datum `{date_val}` v historii "
                        f"`{name}` pro kit `{kit_key}`.",
                        payload={
                            "username": name,
                            "kit": kit_key,
                            "date": date_val,
                            "tier": code,
                        },
                    )
                    continue
                if tier is None:
                    report["history_unknown_tier"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_HISTORY_UNKNOWN_TIER,
                        source_file=players_path,
                        source_key=f"{name}:{kit_key}:{code}",
                        reason=f"Neznámý tier code `{code}` v historii `{name}` "
                        f"pro kit `{kit_key}` (není v modes ani historii).",
                        payload={"username": name, "kit": kit_key, "tier": code},
                    )
                    continue
                if await _history_exists(
                    session,
                    player_id=player.id,
                    kit_id=kit.id,
                    tier_id=tier.id,
                    changed_at=changed_at,
                ):
                    report["history_skipped_existing"] += 1
                    continue
                session.add(
                    TierHistory(
                        player_id=player.id,
                        kit_id=kit.id,
                        tier_id=tier.id,
                        previous_tier_id=None,  # no reconstruction (see module docstring)
                        changed_at=changed_at,
                        source="migration",
                        reason=HISTORY_REASON,
                    )
                )
                report["history_imported"] += 1


def _load_result_kit_index(data_dir: Path) -> dict[tuple[str, int], set[str]]:
    """Index legacy results by ``(discord_id, timestamp_ms)`` -> {kit_key}.

    Legacy ``cooldowns.json`` is kit-less (``{discord_id: expires_ms}``) —
    the old bot had ONE global 4-day cooldown. The kit is nevertheless
    *derivable*: ``cooldowns[playerId] = now`` was written in the very same
    transaction as the result that triggered it, and that result records both
    ``playerId``, ``kit`` and ``timestamp = now``. So an exact
    ``(playerId, timestamp)`` match is a derivation, not a guess.

    Only *unambiguous* matches count — see :func:`_resolve_cooldown_kit`.
    A missing/unreadable file yields an empty index, which degrades safely:
    every cooldown then falls back to the legacy global row + an open issue.
    """
    path = data_dir / SRC_RESULTS
    index: dict[tuple[str, int], set[str]] = {}
    if not path.exists():
        return index
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return index
    if not isinstance(raw, dict):
        return index
    for record in raw.values():
        if not isinstance(record, dict):
            continue
        player_id = str(record.get("playerId") or "").strip()
        kit = str(record.get("kit") or "").strip()
        if not player_id or not kit:
            continue
        try:
            ts = int(record.get("timestamp"))
        except (TypeError, ValueError):
            continue
        index.setdefault((player_id, ts), set()).add(kit)
    return index


# ``_resolve_cooldown_kit`` status values.
KIT_OK = "attributed"
KIT_NO_RESULT = "no_result"
KIT_AMBIGUOUS = "ambiguous"
KIT_UNKNOWN_REGISTRY = "unknown_registry"


def _resolve_cooldown_kit(
    index: dict[tuple[str, int], set[str]], discord_id: int, ms
) -> tuple[Optional[str], str]:
    """Resolve the kit of a legacy kit-less waitlist cooldown.

    Returns ``(kit_key, status)``. ``kit_key`` is only non-``None`` when
    ``status == KIT_OK``, i.e. when exactly one distinct kit produced the
    cooldown. Every other outcome keeps the row global (never deleted, never
    guessed) and is reported as an explicit migration issue.
    """
    try:
        ts = int(ms)
    except (TypeError, ValueError):
        return None, KIT_NO_RESULT
    kits = index.get((str(discord_id), ts))
    if not kits:
        return None, KIT_NO_RESULT
    if len(kits) > 1:
        return None, KIT_AMBIGUOUS
    return next(iter(kits)), KIT_OK


async def _lookup_kit(
    kit_repo: KitRepository, session: AsyncSession, name: str
) -> Optional[Kit]:
    """Exact kit lookup for a legacy name — by key, then by display name.

    Legacy ``ht_results.json`` stores the kit *display name* while ``kits.json``
    seeds ``Kit.key``. The case-insensitive name match is exact (not fuzzy) and
    is the same comparison the ticket/result validation already performs; a kit
    that resolves to nothing is reported, never approximated.
    """
    kit = await kit_repo.get_by_key(session, name)
    return kit if kit is not None else await kit_repo.get_by_name(session, name)


async def _import_link_keyed_stores(
    session: AsyncSession,
    *,
    report: dict,
    data_dir: Path,
    player_repo: PlayerRepository,
    kit_repo: KitRepository,
    cooldown_repo: CooldownRepository,
) -> None:
    path = data_dir / SRC_COOLDOWNS
    result_kit_index = _load_result_kit_index(data_dir)
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key_raw, ms in raw.items():
                try:
                    discord_id = int(key_raw)
                except (TypeError, ValueError):
                    report["cooldowns_invalid_key"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_COOLDOWN_INVALID_KEY,
                        source_file=SRC_COOLDOWNS,
                        source_key=str(key_raw),
                        reason=f"Discord ID klíč `{key_raw}` není číslo.",
                        payload={"key": str(key_raw)},
                    )
                    continue
                player = await player_repo.get_by_discord_id(session, discord_id)
                if player is None:
                    report["cooldowns_unresolved"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_COOLDOWN_UNRESOLVED,
                        source_file=SRC_COOLDOWNS,
                        source_key=str(discord_id),
                        reason="Discord ID není navázané na žádného hráče "
                        f"(`{discord_id}`) — čeká na /linkdiscord, nic se nehádá.",
                        payload={"discord_id": str(discord_id), "expires_at_ms": ms},
                    )
                    continue
                expires_at = _epoch_ms_to_utc(ms)
                if expires_at is None:
                    report["cooldowns_invalid_expiry"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_COOLDOWN_INVALID_EXPIRY,
                        source_file=SRC_COOLDOWNS,
                        source_key=str(discord_id),
                        reason=f"Nečitelná expirace `{ms}` u cooldownu.",  # noqa: E501
                        payload={"discord_id": str(discord_id), "expires_at_ms": ms},
                    )
                    continue

                # --- Kit attribution -------------------------------------
                # cooldowns.json has no kit dimension. The kit is derivable
                # ONLY when exactly one legacy result at the very same
                # instant produced the cooldown. Anything less is left
                # global and reported — never guessed, never dropped.
                kit_key, kit_status = _resolve_cooldown_kit(
                    result_kit_index, discord_id, ms
                )
                kit_id: Optional[int] = None
                if kit_status == KIT_OK:
                    kit = await _lookup_kit(kit_repo, session, kit_key)
                    if kit is None:
                        kit_status = KIT_UNKNOWN_REGISTRY
                    else:
                        kit_id = kit.id

                if kit_id is not None:
                    report["cooldowns_imported_per_kit"] += 1
                else:
                    report["cooldowns_kept_global"] += 1
                    if kit_status == KIT_UNKNOWN_REGISTRY:
                        report["cooldowns_kit_unknown_registry"] += 1
                        reason = (
                            f"Cooldown hráče `{discord_id}` byl jednoznačně "
                            f"přiřazen ke kitu `{kit_key}`, ale tento kit není "
                            "v registru kits. Řádek zůstává globální (bez kit_id)."
                        )
                        category = CAT_COOLDOWN_KIT_UNKNOWN
                    else:
                        report["cooldowns_kit_unattributable"] += 1
                        if kit_status == KIT_AMBIGUOUS:
                            reason = (
                                f"Cooldown hráče `{discord_id}` nelze jednoznačně "
                                "přiřadit ke kitu: ve stejném okamžiku "
                                f"(timestamp {ms}) existuje více výsledků s různými "
                                "kity. Řádek zůstává globální (bez kit_id)."
                            )
                        else:
                            reason = (
                                f"Cooldown hráče `{discord_id}` nelze přiřadit "
                                f"ke kitu: v {SRC_RESULTS} není žádný výsledek "
                                f"s `playerId` a `timestamp` {ms}. Řádek zůstává "
                                "globální (bez kit_id)."
                            )
                        category = CAT_COOLDOWN_KIT_UNATTRIBUTABLE
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=category,
                        source_file=SRC_COOLDOWNS,
                        source_key=str(discord_id),
                        reason=reason,
                        payload={
                            "discord_id": str(discord_id),
                            "expires_at_ms": ms,
                            "kit_key": kit_key,
                            "attribution": kit_status,
                        },
                    )

                await cooldown_repo.upsert(
                    session,
                    player_id=player.id,
                    cooldown_type=COOLDOWN_WAITLIST,
                    expires_at=expires_at,
                    kit_id=kit_id,
                    source="migration",
                )
                report["cooldowns_imported"] += 1

    path = data_dir / SRC_HT3
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key_raw, kits_map in raw.items():
                try:
                    discord_id = int(key_raw)
                except (TypeError, ValueError):
                    report["ht3_invalid_key"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_HT3_INVALID_KEY,
                        source_file=SRC_HT3,
                        source_key=str(key_raw),
                        reason=f"Discord ID klíč `{key_raw}` není číslo.",
                        payload={"key": str(key_raw)},
                    )
                    continue
                player = await player_repo.get_by_discord_id(session, discord_id)
                if player is None:
                    report["ht3_unresolved_players"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_HT3_UNRESOLVED,
                        source_file=SRC_HT3,
                        source_key=str(discord_id),
                        reason="Discord ID není navázané na žádného hráče "
                        f"(`{discord_id}`) — čeká na /linkdiscord, nic se nehádá.",
                        payload={"discord_id": str(discord_id)},
                    )
                    continue
                if not isinstance(kits_map, dict):
                    continue
                for kit_key, ms in kits_map.items():
                    kit = await kit_repo.get_by_key(session, kit_key)
                    if kit is None:
                        report["ht3_unknown_kit"] += 1
                        report["issues_recorded"] += await _record_issue(
                            session,
                            category=CAT_HT3_UNKNOWN_KIT,
                            source_file=SRC_HT3,
                            source_key=f"{discord_id}:{kit_key}",
                            reason=f"HT3 cooldown pro neznámý kit `{kit_key}` "
                            f"(Discord ID `{discord_id}`).",
                            payload={
                                "discord_id": str(discord_id),
                                "kit": kit_key,
                                "expires_at_ms": ms,
                            },
                        )
                        continue
                    expires_at = _epoch_ms_to_utc(ms)
                    if expires_at is None:
                        report["ht3_invalid_expiry"] = report.get(
                            "ht3_invalid_expiry", 0
                        ) + 1
                        report["issues_recorded"] += await _record_issue(
                            session,
                            category=CAT_COOLDOWN_INVALID_EXPIRY,
                            source_file=SRC_HT3,
                            source_key=f"{discord_id}:{kit_key}",
                            reason=f"Nečitelná expirace `{ms}` u HT3 cooldownu.",
                            payload={
                                "discord_id": str(discord_id),
                                "kit": kit_key,
                                "expires_at_ms": ms,
                            },
                        )
                        continue
                    await cooldown_repo.upsert(
                        session,
                        player_id=player.id,
                        cooldown_type=COOLDOWN_HT3,
                        expires_at=expires_at,
                        kit_id=kit.id,
                        source="migration",
                    )
                    report["ht3_imported"] += 1

    path = data_dir / SRC_TESTERS
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for val in raw:
                try:
                    discord_id = int(val)
                except (TypeError, ValueError):
                    report["testers_invalid_key"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_TESTER_INVALID_KEY,
                        source_file=SRC_TESTERS,
                        source_key=str(val),
                        reason=f"Discord ID `{val}` v testers.json není číslo.",
                        payload={"value": str(val)},
                    )
                    continue
                player = await player_repo.get_by_discord_id(session, discord_id)
                if player is None:
                    report["testers_unresolved"] += 1
                    report["issues_recorded"] += await _record_issue(
                        session,
                        category=CAT_TESTER_UNRESOLVED,
                        source_file=SRC_TESTERS,
                        source_key=str(discord_id),
                        reason="Discord ID není navázané na žádného hráče "
                        f"(`{discord_id}`) — čeká na /linkdiscord, nic se nehádá.",
                        payload={"discord_id": str(discord_id)},
                    )
                    continue
                if await session.get(Tester, player.id) is not None:
                    report["testers_existing"] += 1
                    continue
                session.add(Tester(player_id=player.id, granted_by=None))
                report["testers_imported"] += 1


async def run_import(session: AsyncSession, *, data_dir: Path) -> dict:
    """Import all legacy stores into the relational schema (one transaction)."""
    report: dict = {
        "data_dir": str(data_dir),
        "kits_ensured": 0,
        "tiers_ensured": 0,
        "players_created": 0,
        "players_existing": 0,
        "players_no_username": 0,
        "players_malformed": 0,
        "history_imported": 0,
        "history_skipped_existing": 0,
        "history_invalid_dates": 0,
        "history_unknown_kit": 0,
        "history_unknown_tier": 0,
        "cooldowns_imported": 0,
        "cooldowns_imported_per_kit": 0,
        "cooldowns_kept_global": 0,
        "cooldowns_kit_unattributable": 0,
        "cooldowns_kit_unknown_registry": 0,
        "cooldowns_unresolved": 0,
        "cooldowns_invalid_key": 0,
        "cooldowns_invalid_expiry": 0,
        "ht3_imported": 0,
        "ht3_unresolved_players": 0,
        "ht3_unknown_kit": 0,
        "ht3_invalid_key": 0,
        "ht3_invalid_expiry": 0,
        "testers_imported": 0,
        "testers_existing": 0,
        "testers_unresolved": 0,
        "testers_invalid_key": 0,
        "issues_recorded": 0,
    }

    players_path = data_dir / SRC_PLAYERS
    if not players_path.exists():
        raise FileNotFoundError(
            f"{SRC_PLAYERS} neexistuje v {data_dir} — import se zastavuje."
        )
    raw_players = json.loads(players_path.read_text(encoding="utf-8"))
    if not isinstance(raw_players, list):
        raise ValueError(f"{SRC_PLAYERS} musí být JSON list.")

    kit_repo = KitRepository()
    tier_repo = TierDefinitionRepository()
    player_repo = PlayerRepository()
    cooldown_repo = CooldownRepository()

    kits_path = data_dir / "kits.json"
    if kits_path.exists():
        raw_kits = json.loads(kits_path.read_text(encoding="utf-8"))
        if isinstance(raw_kits, list):
            for key in raw_kits:
                await kit_repo.get_or_create(session, key=key, name=key)
                report["kits_ensured"] += 1

    await _import_players_and_history(
        session,
        report=report,
        records=raw_players,
        player_repo=player_repo,
        kit_repo=kit_repo,
        tier_repo=tier_repo,
    )
    await _import_link_keyed_stores(
        session,
        report=report,
        data_dir=data_dir,
        player_repo=player_repo,
        kit_repo=kit_repo,
        cooldown_repo=cooldown_repo,
    )

    # markers (same transaction — report and trail commit atomically)
    await BotConfigRepository().set(
        session,
        BOT_CONFIG_IMPORT_KEY,
        {
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "report": report,
        },
    )
    await AuditRepository().append(
        session,
        action=AUDIT_ACTION_IMPORT,
        entity_type="import",
        entity_id=str(data_dir),
        details={"report": {k: v for k, v in report.items() if k != "data_dir"}},
    )
    return report


async def import_json_data(session_factory, *, data_dir: Path) -> dict:
    """Public entry: single transaction, commit-or-rollback."""
    async with transaction(session_factory) as session:
        return await run_import(session, data_dir=data_dir)
"""Legacy JSON → normalized PostgreSQL import (one idempotent command).

    python -m tools.legacy_import --preview      # backup + dry run (rollback)
    python -m tools.legacy_import --apply        # backup + import (commit)

Sources
-------
* ``data/*.json`` (the old JSON-mode files) and
* the ``dachshundtiers_data`` key → JSONB table in the target database (the
  store the bot wrote to while it ran in "PostgreSQL = JSON documents" mode).

When the same key exists in both, the table wins (it is the newer runtime
store) and the difference is reported as a conflict.

Rules
-----
* **Backup first, always.** Every source document, every row of every
  normalized table and a SHA-256 manifest go to ``backups/legacy_import/<UTC>``
  before anything is written.
* **Fill gaps, never overwrite.** Existing database state wins: a kit role
  mapping, cooldown, evaluation, tester or result that already exists is left
  untouched. Current tiers are never imported — Discord is their authority
  (``/sync import-discord``).
* **Idempotent.** Natural keys everywhere (runtime-compatible
  ``result:{id}`` / ``ht_fight:{id}`` result keys, history by
  player+kit+tier+date, credits via a stored marker). A second ``--apply``
  changes nothing.
* **Nothing is lost.** Every source document is archived verbatim in
  ``audit_logs`` (``action='legacy_archive'``), including stores that have no
  relational target (old ticket state, sync logs).
* **One transaction.** ``--preview`` runs the complete import and rolls it
  back; ``--apply`` commits it. Any error rolls everything back.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.base import Base
from db.models import (
    AuditLog,
    Cooldown,
    Evaluation,
    Kit,
    KitRole,
    Player,
    Result,
    Tester,
    TierHistory,
)
from db.repositories.cooldowns import COOLDOWN_HT3, COOLDOWN_WAITLIST, CooldownRepository
from db.repositories.kits import KitRepository, KitRoleRepository
from db.repositories.players import PlayerIdentityError, PlayerRepository
from db.repositories.sync_audit import AuditRepository, BotConfigRepository
from db.repositories.tester_credits import TesterCreditRepository
from db.repositories.tiers import MirrorRepository
from db.tier_catalog import ensure_canonical_tiers, ensure_tier

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_BACKUP_ROOT = REPO_ROOT / "backups" / "legacy_import"

KV_TABLE = "dachshundtiers_data"
ARCHIVE_ACTION = "legacy_archive"
IMPORT_ACTION = "legacy_import"
CREDIT_MARKER_KEY = "legacy_import.tester_credits"
LAST_RUN_KEY = "legacy_import.last_run"
LEGACY_MONTH = "legacy"

PRAGUE = ZoneInfo("Europe/Prague")
_DATE_RE = re.compile(r"^\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*$")
_EMPTY_TIERS = {"", "N/A", "NONE", "NULL", "-"}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
@dataclass
class SourceDoc:
    name: str
    origin: str  # "kv" | "file"
    data: Any
    sha256: str
    raw: bytes


@dataclass
class Sources:
    docs: dict[str, SourceDoc] = field(default_factory=dict)
    shadowed: list[SourceDoc] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unreadable: list[dict] = field(default_factory=list)

    def get(self, name: str, default=None):
        doc = self.docs.get(name)
        return default if doc is None else doc.data


def _canonical(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


async def kv_table_exists(session: AsyncSession) -> bool:
    return (
        await session.execute(text("SELECT to_regclass(:t)"), {"t": KV_TABLE})
    ).scalar_one() is not None


async def load_sources(
    session: AsyncSession, data_dir: Optional[Path], *, use_kv: bool = True
) -> Sources:
    sources = Sources()
    files: dict[str, SourceDoc] = {}
    if data_dir is not None and data_dir.is_dir():
        for path in sorted(data_dir.glob("*.json")):
            raw = path.read_bytes()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as err:
                sources.unreadable.append({"name": path.name, "origin": "file", "error": str(err)})
                continue
            files[path.name] = SourceDoc(path.name, "file", data, _sha(raw), raw)

    kv: dict[str, SourceDoc] = {}
    if use_kv and await kv_table_exists(session):
        rows = await session.execute(text(f"SELECT key, value FROM {KV_TABLE} ORDER BY key"))
        for key, value in rows.all():
            raw = _canonical(value)
            kv[key] = SourceDoc(key, "kv", value, _sha(raw), raw)

    for name in sorted(set(files) | set(kv)):
        if name in kv:
            sources.docs[name] = kv[name]
            if name in files:
                sources.shadowed.append(files[name])
                if _canonical(files[name].data) != kv[name].raw:
                    sources.conflicts.append(name)
        else:
            sources.docs[name] = files[name]
    return sources


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def write_backup(session: AsyncSession, sources: Sources, root: Path) -> Path:
    """Source documents + full dump of all normalized tables + manifest."""
    dest = root / _utc_stamp()
    suffix = 1
    while dest.exists():
        dest = root / f"{_utc_stamp()}-{suffix}"
        suffix += 1
    (dest / "sources").mkdir(parents=True)
    (dest / "tables").mkdir()
    manifest: dict = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
                      "sources": [], "tables": {}}

    for doc in list(sources.docs.values()) + sources.shadowed:
        out = dest / "sources" / doc.origin / doc.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(doc.raw)
        manifest["sources"].append(
            {"name": doc.name, "origin": doc.origin, "sha256": doc.sha256,
             "size_bytes": len(doc.raw)}
        )
    if await kv_table_exists(session):
        rows = await session.execute(text(f"SELECT key, value, updated_at FROM {KV_TABLE}"))
        dump = [{"key": k, "value": v, "updated_at": _jsonable(u)} for k, v, u in rows.all()]
        raw = json.dumps(dump, ensure_ascii=False, indent=2).encode("utf-8")
        (dest / "tables" / f"{KV_TABLE}.json").write_bytes(raw)
        manifest["tables"][KV_TABLE] = {"rows": len(dump), "sha256": _sha(raw)}

    for table in Base.metadata.sorted_tables:
        rows = (await session.execute(select(table))).mappings().all()
        dump = [{k: _jsonable(v) for k, v in row.items()} for row in rows]
        raw = json.dumps(dump, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        (dest / "tables" / f"{table.name}.json").write_bytes(raw)
        manifest["tables"][table.name] = {"rows": len(dump), "sha256": _sha(raw)}

    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.counts: Counter = Counter()
        self.issues: list[dict] = []

    def inc(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def issue(self, category: str, key: str, reason: str) -> None:
        self.issues.append({"category": category, "key": key, "reason": reason})
        self.counts[f"issue:{category}"] += 1

    def as_dict(self) -> dict:
        return {"counts": dict(sorted(self.counts.items())), "issues": self.issues}


def _ms_to_dt(value) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_legacy_date(value) -> Optional[datetime]:
    """'DD.MM.YYYY' (also 'D. M. YYYY') → UTC midnight."""
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value)
    if match is None:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_int(value) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Importer:
    def __init__(self, session: AsyncSession, sources: Sources, report: Report) -> None:
        self.s = session
        self.src = sources
        self.r = report
        self.players = PlayerRepository()
        self.kits = KitRepository()
        self._kit_cache: dict[str, Kit] = {}

    # --- helpers --------------------------------------------------------
    async def kit_for(self, name, *, create_inactive: bool = True) -> Optional[Kit]:
        clean = str(name or "").strip()
        if not clean:
            return None
        cache_key = clean.lower()
        if cache_key in self._kit_cache:
            return self._kit_cache[cache_key]
        kit = await self.kits.get_by_key(self.s, cache_key) or await self.kits.get_by_name(
            self.s, clean
        )
        if kit is None and create_inactive:
            kit = Kit(key=cache_key, name=clean, active=False)
            self.s.add(kit)
            await self.s.flush()
            self.r.inc("kits_created_inactive")
        if kit is not None:
            self._kit_cache[cache_key] = kit
        return kit

    async def tier_for(self, code) -> Optional[Any]:
        clean = str(code or "").strip()
        if clean.upper() in _EMPTY_TIERS:
            return None
        return await ensure_tier(self.s, clean)

    async def player_for_discord(self, discord_id: int, ign_hint: str = "") -> Player:
        """Existing player by Discord ID; else claim with the legacy IGN; else a
        placeholder IGN that the player's next claim renames automatically."""
        player = await self.players.get_by_discord_id(self.s, discord_id)
        if player is not None:
            return player
        hint = (ign_hint or "").strip()
        if hint:
            try:
                _claim, player = await self.players.claim_discord_id(
                    self.s, discord_id=discord_id, ign=hint
                )
                self.r.inc("players_created_from_discord_id")
                return player
            except PlayerIdentityError:
                pass
        _claim, player = await self.players.claim_discord_id(
            self.s, discord_id=discord_id, ign=f"discord-{discord_id}"
        )
        self.r.inc("players_created_placeholder")
        return player

    # --- steps ----------------------------------------------------------
    async def kits_step(self) -> None:
        for name in self.src.get("kits.json", []) or []:
            if not str(name or "").strip():
                continue
            existing = await self.kit_for(name, create_inactive=False)
            if existing is None:
                kit = Kit(key=str(name).strip().lower(), name=str(name).strip(), active=True)
                self.s.add(kit)
                await self.s.flush()
                self._kit_cache[kit.key] = kit
                self.r.inc("kits_created")
            else:
                self.r.inc("kits_existing")

    async def kit_roles_step(self) -> None:
        raw = self.src.get("kit_roles.json", {}) or {}
        if not isinstance(raw, dict):
            return
        roles = KitRoleRepository()
        for kit_name, tier_map in raw.items():
            if not isinstance(tier_map, dict):
                self.r.issue("kit_roles_malformed", str(kit_name), "Hodnota není objekt tier → role.")
                continue
            kit = await self.kit_for(kit_name)
            for code, role_id in tier_map.items():
                rid = _as_int(role_id)
                tier = await self.tier_for(code)
                if rid is None or tier is None:
                    self.r.issue("kit_roles_invalid", f"{kit_name}:{code}", f"Neplatná role `{role_id}`.")
                    continue
                pair = (await self.s.execute(
                    select(KitRole).where(KitRole.kit_id == kit.id, KitRole.tier_id == tier.id)
                )).scalar_one_or_none()
                if pair is not None:
                    self.r.inc("kit_roles_existing")
                    continue
                if await roles.get_by_role(self.s, rid) is not None:
                    self.r.issue(
                        "kit_roles_role_taken", f"{kit_name}:{code}",
                        f"Role `{rid}` už je v DB namapovaná jinam – nepřepisuje se.",
                    )
                    continue
                await roles.set_mapping(self.s, kit_id=kit.id, tier_id=tier.id, discord_role_id=rid)
                self.r.inc("kit_roles_created")

    async def players_step(self) -> None:
        records = self.src.get("players.json", []) or []
        if not isinstance(records, list):
            self.r.issue("players_malformed", "players.json", "Soubor není JSON list.")
            return
        for idx, rec in enumerate(records):
            if not isinstance(rec, dict):
                self.r.issue("player_malformed", f"#{idx}", "Záznam není objekt.")
                continue
            name = str(rec.get("username") or "").strip()
            did = _as_int(rec.get("discordId"))
            if not name and did is None:
                self.r.issue("player_no_identity", f"#{idx}", "Záznam nemá username ani discordId.")
                continue
            if did is not None:
                player = await self.player_for_discord(did, name)
            else:
                player = await self.players.get_by_ign(self.s, name)
                if player is None:
                    player = await self.players.get_or_create_by_ign(self.s, ign=name)
                    self.r.inc("players_created")
                else:
                    self.r.inc("players_existing")
            await self._history(player, rec.get("history"), label=name or str(did))
            await self._modes_report(player, rec.get("modes"))

    async def _history(self, player: Player, history, *, label: str) -> None:
        if not isinstance(history, dict):
            return
        for kit_name, entries in history.items():
            if not isinstance(entries, list):
                continue
            kit = await self.kit_for(kit_name)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                changed_at = parse_legacy_date(entry.get("date"))
                tier = await self.tier_for(entry.get("tier"))
                if changed_at is None or tier is None:
                    self.r.issue(
                        "history_invalid", f"{label}:{kit_name}",
                        f"Nečitelný záznam historie {entry!r}.",
                    )
                    continue
                exists = (await self.s.execute(
                    select(TierHistory.id).where(
                        TierHistory.player_id == player.id,
                        TierHistory.kit_id == kit.id,
                        TierHistory.tier_id == tier.id,
                        TierHistory.changed_at == changed_at,
                    ).limit(1)
                )).first()
                if exists is not None:
                    self.r.inc("history_existing")
                    continue
                self.s.add(TierHistory(
                    player_id=player.id, kit_id=kit.id, tier_id=tier.id,
                    previous_tier_id=None, changed_at=changed_at, source="migration",
                    reason="legacy import (players.json history)",
                ))
                self.r.inc("history_created")
        await self.s.flush()

    async def _modes_report(self, player: Player, modes) -> None:
        """Current tiers are Discord's; only report what the mirror lacks."""
        if not isinstance(modes, dict):
            return
        for kit_name, code in modes.items():
            kit = await self.kit_for(kit_name)
            if await MirrorRepository().get_current(
                self.s, player_id=player.id, kit_id=kit.id
            ) is None:
                self.r.inc("modes_missing_in_mirror")
                if code is not None:
                    await self.tier_for(code)

    def _result_records(self) -> list[dict]:
        raw = self.src.get("ht_results.json", {}) or {}
        if isinstance(raw, dict):
            items = [dict(v, id=v.get("id") or k) for k, v in raw.items() if isinstance(v, dict)]
        elif isinstance(raw, list):
            items = [v for v in raw if isinstance(v, dict)]
        else:
            items = []
        return items

    async def results_step(self) -> None:
        for rec in self._result_records():
            rid = str(rec.get("id") or "").strip()
            is_fight = str(rec.get("resultType") or "") == "ht_fight" or rec.get("kind") == "ht_fight"
            key = f"{'ht_fight' if is_fight else 'result'}:{rid}"
            if not rid:
                self.r.issue("result_no_id", json.dumps(rec)[:80], "Výsledek nemá id.")
                continue
            if await ResultRepoLite.exists(self.s, key):
                self.r.inc("results_existing")
                continue
            pid = _as_int(rec.get("playerId"))
            kit = await self.kit_for(rec.get("kit"))
            recorded_at = _ms_to_dt(rec.get("timestamp")) or parse_legacy_date(rec.get("date"))
            if pid is None or kit is None or recorded_at is None:
                self.r.issue("result_incomplete", rid, "Chybí hráč, kit nebo čas výsledku.")
                continue
            player = await self.player_for_discord(pid, rec.get("ign") or rec.get("playerName") or "")
            evaluator = None
            eid = _as_int(rec.get("evaluatorId"))
            if eid is not None:
                evaluator = await self.player_for_discord(eid, "")
            prev = await self.tier_for(rec.get("previousTier"))
            new = await self.tier_for(rec.get("newTier"))
            bridge = await self.tier_for(rec.get("bridgeTier"))
            kind = "ht_fight" if is_fight else (
                rec.get("kind") if rec.get("kind") in ("ticket", "queue") else
                ("ticket" if rec.get("ticketId") else "queue")
            )
            announcement = rec.get("announcement")
            self.s.add(Result(
                result_key=key,
                kind=kind,
                subtype=str(rec.get("fightTier") or "").strip() or None if is_fight else None,
                player_id=player.id,
                evaluator_id=evaluator.id if evaluator is not None else None,
                kit_id=kit.id,
                ticket_channel_id=_as_int(rec.get("ticketId")),
                previous_tier_id=prev.id if prev is not None else None,
                new_tier_id=new.id if new is not None else None,
                bridge_tier_id=bridge.id if bridge is not None else None,
                tier_status=(rec.get("tierStatus") or None),
                score=(str(rec.get("score")) if rec.get("score") not in (None, "") else None),
                outcome=(rec.get("outcome") or None),
                opponent_id=_as_int(rec.get("opponentId")),
                opponent_name=(rec.get("opponentName") or None),
                notes=(rec.get("notes") or None),
                eval_flag=bool(rec.get("eval")),
                date=(rec.get("date") or None),
                recorded_at=recorded_at,
                promotion_status=None,
                announcement_status=announcement if announcement in ("pending", "sent", "failed") else None,
            ))
            await self.s.flush()
            self.r.inc("results_created")

    async def evals_step(self) -> None:
        raw = self.src.get("evals.json", {}) or {}
        if not isinstance(raw, dict):
            return
        for kit_name, bucket in raw.items():
            if not isinstance(bucket, dict):
                continue
            kit = await self.kit_for(kit_name)
            for ign, ms in bucket.items():
                player = await self.players.get_by_ign(self.s, str(ign))
                if player is None:
                    self.r.issue("eval_unknown_player", f"{kit_name}:{ign}", "Hráč s tímto IGN v DB není.")
                    continue
                any_row = (await self.s.execute(
                    select(Evaluation.id).where(
                        Evaluation.player_id == player.id, Evaluation.kit_id == kit.id
                    ).limit(1)
                )).first()
                if any_row is not None:
                    # DB už eval pro tuto dvojici zná (i odebraný) – nepřepisuje se.
                    self.r.inc("evals_existing")
                    continue
                self.s.add(Evaluation(
                    player_id=player.id, kit_id=kit.id,
                    granted_at=_ms_to_dt(ms) or datetime.now(timezone.utc),
                ))
                self.r.inc("evals_created")
        await self.s.flush()

    async def testers_step(self) -> None:
        raw = self.src.get("testers.json", []) or []
        if not isinstance(raw, list) or not raw:
            return
        if (await self.s.execute(select(func.count()).select_from(Tester))).scalar_one():
            # Testeři se v DB spravují od cutoveru; odebraný tester by se jinak vrátil.
            self.r.inc("testers_skipped_db_authoritative", len(raw))
            return
        for value in raw:
            did = _as_int(value)
            if did is None:
                self.r.issue("tester_invalid_id", str(value), "Discord ID není číslo.")
                continue
            player = await self.player_for_discord(did)
            if await self.s.get(Tester, player.id) is None:
                self.s.add(Tester(player_id=player.id))
                self.r.inc("testers_created")
        await self.s.flush()

    def _result_kit_index(self) -> dict[tuple[str, int], set[str]]:
        index: dict[tuple[str, int], set[str]] = {}
        for rec in self._result_records():
            pid = str(rec.get("playerId") or "").strip()
            ts = _as_int(rec.get("timestamp"))
            kit = str(rec.get("kit") or "").strip()
            if pid and ts is not None and kit:
                index.setdefault((pid, ts), set()).add(kit)
        return index

    async def _upsert_cooldown(self, player: Player, ctype: str, kit_id, expires_at) -> None:
        existing = (await self.s.execute(
            select(Cooldown.expires_at).where(
                Cooldown.player_id == player.id,
                Cooldown.cooldown_type == ctype,
                Cooldown.kit_id.is_(None) if kit_id is None else Cooldown.kit_id == kit_id,
            )
        )).scalar_one_or_none()
        if existing is not None and existing >= expires_at:
            self.r.inc("cooldowns_existing")
            return
        await CooldownRepository().upsert(
            self.s, player_id=player.id, cooldown_type=ctype,
            expires_at=expires_at, kit_id=kit_id, source="migration",
        )
        self.r.inc("cooldowns_created")

    async def cooldowns_step(self, now: datetime) -> None:
        index = self._result_kit_index()
        raw = self.src.get("cooldowns.json", {}) or {}
        for key, ms in (raw.items() if isinstance(raw, dict) else []):
            did, expires_at = _as_int(key), _ms_to_dt(ms)
            if did is None or expires_at is None:
                self.r.issue("cooldown_invalid", str(key), f"Nečitelný cooldown `{ms}`.")
                continue
            if expires_at <= now:
                self.r.inc("cooldowns_expired_skipped")
                continue
            kits = index.get((str(did), _as_int(ms)), set())
            kit = await self.kit_for(next(iter(kits))) if len(kits) == 1 else None
            player = await self.player_for_discord(did)
            await self._upsert_cooldown(player, COOLDOWN_WAITLIST, kit.id if kit else None, expires_at)

        raw = self.src.get("ht3_cooldowns.json", {}) or {}
        for key, per_kit in (raw.items() if isinstance(raw, dict) else []):
            did = _as_int(key)
            if did is None or not isinstance(per_kit, dict):
                self.r.issue("ht3_cooldown_invalid", str(key), "Nečitelný HT3 cooldown.")
                continue
            for kit_name, ms in per_kit.items():
                expires_at = _ms_to_dt(ms)
                if expires_at is None:
                    self.r.issue("ht3_cooldown_invalid", f"{key}:{kit_name}", f"Nečitelná expirace `{ms}`.")
                    continue
                if expires_at <= now:
                    self.r.inc("cooldowns_expired_skipped")
                    continue
                kit = await self.kit_for(kit_name)
                player = await self.player_for_discord(did)
                await self._upsert_cooldown(player, COOLDOWN_HT3, kit.id, expires_at)

    async def tester_stats_step(self) -> None:
        """testers_stats.json → tester_credits (only what results don't cover).

        Per tester and month the ledger gets ``max(0, json − results in DB)``.
        What a previous run already credited is kept in ``bot_config``, so a
        re-run only adds the difference — and admin ``/addtest`` credits in
        the same month are never touched.
        """
        raw = self.src.get("testers_stats.json", {}) or {}
        if not isinstance(raw, dict) or not raw:
            return
        cfg = BotConfigRepository()
        marker = dict(await cfg.get(self.s, CREDIT_MARKER_KEY, {}) or {})
        credits = TesterCreditRepository()
        for key, stats in raw.items():
            did = _as_int(key)
            if did is None or not isinstance(stats, dict):
                self.r.issue("tester_stats_invalid", str(key), "Nečitelný záznam statistik.")
                continue
            player = await self.player_for_discord(did)
            monthly = {
                m: _as_int(v) or 0
                for m, v in (stats.get("monthly") or {}).items()
                if isinstance(m, str)
            }
            total = _as_int(stats.get("total")) or 0
            legacy_rest = max(0, total - sum(monthly.values()))
            db_months = await self._result_months(player.id)
            done = dict(marker.get(str(did), {}))
            for month, amount in list(monthly.items()) + [(LEGACY_MONTH, legacy_rest)]:
                target = max(0, amount - db_months.get(month, 0))
                delta = target - int(done.get(month, 0))
                if delta:
                    await credits.credit(self.s, tester_id=player.id, month=month, amount=delta)
                    self.r.inc("tester_credit_units", delta)
                done[month] = target
            marker[str(did)] = done
        await cfg.set(self.s, CREDIT_MARKER_KEY, marker)

    async def _result_months(self, player_id: int) -> dict[str, int]:
        rows = await self.s.execute(
            select(Result.recorded_at).where(
                Result.evaluator_id == player_id, Result.kind.in_(("ticket", "queue"))
            )
        )
        out: Counter = Counter()
        for (recorded_at,) in rows:
            out[recorded_at.astimezone(PRAGUE).strftime("%m.%Y")] += 1
        return dict(out)

    async def archive_step(self) -> None:
        audit = AuditRepository()
        for doc in list(self.src.docs.values()) + self.src.shadowed:
            exists = (await self.s.execute(
                select(AuditLog.id).where(
                    AuditLog.action == ARCHIVE_ACTION,
                    AuditLog.entity_id == doc.name,
                    AuditLog.details["sha256"].astext == doc.sha256,
                ).limit(1)
            )).first()
            if exists is not None:
                self.r.inc("archive_existing")
                continue
            await audit.append(
                self.s, action=ARCHIVE_ACTION, entity_type="legacy_json",
                entity_id=doc.name,
                details={"origin": doc.origin, "sha256": doc.sha256, "data": doc.data},
            )
            self.r.inc("archive_created")

    async def run(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        await ensure_canonical_tiers(self.s)
        await self.kits_step()
        await self.kit_roles_step()
        await self.players_step()
        await self.results_step()
        await self.evals_step()
        await self.testers_step()
        await self.cooldowns_step(now)
        await self.tester_stats_step()
        await self.archive_step()
        handled = {
            "kits.json", "kit_roles.json", "players.json", "ht_results.json", "evals.json",
            "testers.json", "cooldowns.json", "ht3_cooldowns.json", "testers_stats.json",
        }
        self.r.counts["archived_only_keys"] = len(set(self.src.docs) - handled)


class ResultRepoLite:
    @staticmethod
    async def exists(session: AsyncSession, key: str) -> bool:
        return (await session.execute(
            select(Result.id).where(Result.result_key == key).limit(1)
        )).first() is not None


def _is_noop(report: Report) -> bool:
    """True when a run changed nothing (only *_existing / skipped counters)."""
    writes = ("created", "credit_units")
    return not any(
        v for k, v in report.counts.items()
        if any(w in k for w in writes)
    )


async def run_legacy_import(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    data_dir: Optional[Path] = DEFAULT_DATA_DIR,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    apply: bool = False,
    use_kv: bool = True,
    now: Optional[datetime] = None,
) -> dict:
    """Backup → import in one transaction → commit (apply) or rollback."""
    async with session_factory() as session:
        async with session.begin():
            sources = await load_sources(session, data_dir, use_kv=use_kv)
            backup_dir = await write_backup(session, sources, backup_root)

    report = Report()
    async with session_factory() as session:
        trans = await session.begin()
        try:
            await Importer(session, sources, report).run(now)
            summary = {
                "mode": "apply" if apply else "preview",
                "backup_dir": str(backup_dir),
                "sources": {n: d.origin for n, d in sources.docs.items()},
                "conflicts_kv_over_file": sources.conflicts,
                "unreadable": sources.unreadable,
                **report.as_dict(),
                "noop": _is_noop(report),
            }
            if apply:
                await BotConfigRepository().set(session, LAST_RUN_KEY, {
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "backup_dir": backup_dir.name,
                    "counts": summary["counts"],
                })
                await AuditRepository().append(
                    session, action=IMPORT_ACTION, entity_type="import",
                    entity_id=backup_dir.name,
                    details={"counts": summary["counts"], "issues": len(report.issues)},
                )
        except BaseException:
            await trans.rollback()
            raise
        if apply:
            await trans.commit()
        else:
            await trans.rollback()

    name = "apply_report.json" if apply else "preview_report.json"
    (backup_dir / name).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return summary


def _print_summary(summary: dict) -> None:
    print(f"Režim: {summary['mode']}   Záloha: {summary['backup_dir']}")
    print("Zdroje: " + ", ".join(f"{n} ({o})" for n, o in summary["sources"].items()))
    if summary["conflicts_kv_over_file"]:
        print("Konflikty (tabulka má přednost před souborem): "
              + ", ".join(summary["conflicts_kv_over_file"]))
    if summary["unreadable"]:
        print(f"Nečitelné soubory: {summary['unreadable']}")
    print("\nPočty:")
    for key, value in summary["counts"].items():
        print(f"  {key:40s} {value}")
    if summary["issues"]:
        print(f"\nProblémy k ruční kontrole ({len(summary['issues'])}):")
        for issue in summary["issues"][:50]:
            print(f"  [{issue['category']}] {issue['key']}: {issue['reason']}")
        if len(summary["issues"]) > 50:
            print("  … celý seznam je v reportu v zálohové složce")
    print("\nBeze změn (idempotentní opakování)." if summary["noop"] else "")


async def _main_async(args) -> int:
    from db.config import build_async_database_url
    from db.engine import create_async_engine_from_url, make_session_factory

    url = build_async_database_url()
    if not url:
        print("❌ DATABASE_URL (nebo DB_HOST/DB_NAME/DB_USER/DB_PASSWORD) není nastavené.",
              file=sys.stderr)
        return 2
    engine = create_async_engine_from_url(url)
    try:
        async with engine.connect() as conn:
            ok = (await conn.execute(text("SELECT to_regclass('players')"))).scalar_one()
        if ok is None:
            print("❌ Schéma chybí – nejdřív spusť `alembic upgrade head`.", file=sys.stderr)
            return 2
        summary = await run_legacy_import(
            make_session_factory(engine),
            data_dir=Path(args.data) if args.data else DEFAULT_DATA_DIR,
            backup_root=Path(args.backup_root) if args.backup_root else DEFAULT_BACKUP_ROOT,
            apply=args.apply,
            use_kv=not args.no_kv,
        )
    finally:
        await engine.dispose()
    _print_summary(summary)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.legacy_import",
        description="Záloha + idempotentní import legacy JSON dat do PostgreSQL.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="záloha + import naprázdno (rollback)")
    mode.add_argument("--apply", action="store_true", help="záloha + import (commit)")
    parser.add_argument("--data", help=f"složka s JSON soubory (default {DEFAULT_DATA_DIR})")
    parser.add_argument("--backup-root", help=f"kam zálohovat (default {DEFAULT_BACKUP_ROOT})")
    parser.add_argument("--no-kv", action="store_true",
                        help=f"nečíst tabulku {KV_TABLE} (jen soubory)")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

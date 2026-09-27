"""Phase D, D1 — hashed backup & verified restore of the JSON data directory.

The backup is byte-for-byte and write-once: every file in the data directory
is copied with ``shutil.copy2`` (timestamps preserved) and a SHA-256 is
computed, so the Phase D completion report can state that the pre-migration
state is bit-identical recoverable.

Semantics (design §16/§17):

* restore NEVER touches Discord. Rollback = restore application persistence,
  leave Discord as-is, re-observe Discord into PostgreSQL.
* a corrupted/unreadable JSON file is still backed up byte-for-byte (forensic
  value) but is flagged ``readable: false`` — the backup never auto-repairs;
* restore refuses to write when any backup hash mismatches (verified before
  the first byte is written; post-write re-verified per file);
* DB markers (``audit_logs`` + ``bot_config``) are written only through an
  explicit session factory and never contain connection strings/passwords.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import storage

MANIFEST_NAME = "manifest.json"
MANIFEST_MD_NAME = "manifest.md"
TOOL_NAME = "phase_d"
MANIFEST_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc_stamp() -> str:
    """UTC timestamp used as backup/report directory name (sortable)."""
    return _utcnow().strftime("%Y%m%dT%H%M%SZ")


def default_backup_root(repo_root: Optional[Path] = None) -> Path:
    """Repo-root ``backups/phase_d/`` (next to the data directory)."""
    root = repo_root or Path(storage.DATA_DIR).resolve().parent
    return root / "backups" / "phase_d"


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_shape(data) -> tuple[Optional[str], Optional[int]]:
    """(kind, record count) for manifest; scalars report ``None`` count."""
    if isinstance(data, list):
        return "list", len(data)
    if isinstance(data, dict):
        return "dict", len(data)
    if data is None:
        return "null", None
    return type(data).__name__, None


def _read_file_for_backup(path: Path) -> dict:
    """Byte hash + parsed metadata for one file; corrupted files are flagged,
    never silently dropped."""
    raw = path.read_bytes()
    entry: dict = {
        "name": path.name,
        "sha256": _sha256_bytes(raw),
        "size_bytes": len(raw),
        "mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "readable": True,
        "json_kind": None,
        "records": None,
    }
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        entry["readable"] = False
        entry["reason"] = f"{type(err).__name__}: {err}"
        return entry
    kind, records = _json_shape(data)
    entry["json_kind"] = kind
    entry["records"] = records
    return entry


def _git_rev(repo_root: Path) -> Optional[str]:
    """Best-effort HEAD commit; ``None`` outside a git worktree."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _alembic_head() -> Optional[str]:
    from db.validation import migration_head

    try:
        return migration_head()
    except Exception:
        return None


def _postgres_table_count() -> Optional[int]:
    """Row count of the JSONB bridge table — WITHOUT revealing credentials."""
    if not storage.using_postgres():
        return None
    try:
        with storage.postgres_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {storage._POSTGRES_TABLE}")
            return int(cur.fetchone()[0])
    except Exception:
        return None


def backup_data_dir(
    data_dir: Optional[Path] = None,
    *,
    dest_root: Optional[Path] = None,
) -> tuple[Path, dict]:
    """Hashed byte-for-byte backup of one data directory.

    Returns ``(backup_dir, manifest)``. Raises :class:`FileNotFoundError` when
    the data directory does not exist — a missing/empty state must never be
    silently backed up as success.
    """
    data = Path(data_dir or storage.DATA_DIR).resolve()
    if not data.is_dir():
        raise FileNotFoundError(f"Data adresář neexistuje: {data}")
    files = sorted(p for p in data.iterdir() if p.is_file() and p.suffix == ".json")
    if not files:
        raise FileNotFoundError(f"V {data} nejsou žádné *.json soubory k zálohování.")

    stamp = utc_stamp()
    dest = Path(dest_root or default_backup_root()) / stamp
    suffix = 1
    while dest.exists():
        dest = dest.with_name(f"{stamp}_{suffix}")
        suffix += 1
    dest.mkdir(parents=True, exist_ok=False)

    entries: list[dict] = []
    total_bytes = 0
    total_records = 0
    for path in files:
        entry = _read_file_for_backup(path)
        shutil.copy2(path, dest / path.name)
        entries.append(entry)
        total_bytes += entry["size_bytes"]
        if entry["records"] is not None:
            total_records += entry["records"]

    repo_root = Path(storage.DATA_DIR).resolve().parent
    manifest = {
        "tool": TOOL_NAME,
        "version": MANIFEST_VERSION,
        "created_at_utc": _utcnow().isoformat(),
        "backend": storage.backend_name(),
        "git_rev": _git_rev(repo_root),
        "alembic_head": _alembic_head(),
        "postgres_table_count": _postgres_table_count(),
        "data_dir": str(data),
        "files": entries,
        "summary": {
            "files": len(entries),
            "bytes": total_bytes,
            "records": total_records,
            "unreadable": sum(1 for e in entries if not e["readable"]),
        },
    }
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest / MANIFEST_MD_NAME).write_text(
        _render_manifest_md(manifest), encoding="utf-8"
    )
    return dest, manifest


def _render_manifest_md(manifest: dict) -> str:
    lines = [
        f"# Phase D backup — {manifest['created_at_utc']}",
        "",
        f"- backend: `{manifest['backend']}`"
        + (f" (JSONB rows `{manifest['postgres_table_count']}`)" if manifest["postgres_table_count"] is not None else ""),
        f"- git: `{manifest.get('git_rev') or 'n/a'}`",
        f"- alembic head: `{manifest.get('alembic_head') or 'n/a'}`",
        f"- files: {manifest['summary']['files']} / bytes: {manifest['summary']['bytes']} / records: {manifest['summary']['records']}",
        "",
        "| file | sha256 | size | records | readable |",
        "|---|---|---|---|---|",
    ]
    for entry in manifest["files"]:
        lines.append(
            f"| {entry['name']} | `{entry['sha256'][:12]}…` | {entry['size_bytes']} "
            f"| {entry['records'] if entry['records'] is not None else '—'} "
            f"| {'yes' if entry['readable'] else '**no**'} |"
        )
    return "\n".join(lines) + "\n"


def load_manifest(backup_dir: Path) -> dict:
    path = Path(backup_dir) / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"Záloha nemá {MANIFEST_NAME}: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("tool") != TOOL_NAME or manifest.get("version") != MANIFEST_VERSION:
        raise ValueError(f"Neznámý formát zálohy: {backup_dir}")
    return manifest


def list_backups(dest_root: Optional[Path] = None) -> list[dict]:
    """Sorted (newest first) backup summaries from ``backups/phase_d/``."""
    root = Path(dest_root or default_backup_root())
    if not root.is_dir():
        return []
    found: list[dict] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            manifest = load_manifest(child)
        except (FileNotFoundError, ValueError):
            continue
        found.append(
            {
                "stamp": child.name,
                "dir": str(child),
                "created_at_utc": manifest.get("created_at_utc"),
                "backend": manifest.get("backend"),
                "git_rev": manifest.get("git_rev"),
                "summary": manifest.get("summary"),
            }
        )
    return sorted(found, key=lambda b: b["stamp"], reverse=True)


def _verify_backup_bytes(backup_dir: Path, manifest: dict) -> list[str]:
    mismatches: list[str] = []
    for entry in manifest["files"]:
        path = Path(backup_dir) / entry["name"]
        if not path.exists():
            mismatches.append(f"{entry['name']}: chybí v záloze")
            continue
        actual = _sha256_bytes(path.read_bytes())
        if actual != entry["sha256"]:
            mismatches.append(f"{entry['name']}: sha256 nesouhlasí")
    return mismatches


def restore_backup(
    backup_dir: Path,
    data_dir: Optional[Path] = None,
    *,
    verify_only: bool = False,
) -> dict:
    """Restore a hashed backup into the data directory (verified).

    Every backed-up file hash is checked BEFORE the first byte is written;
    on any mismatch nothing is written. With ``verify_only`` nothing is ever
    written. Restore never touches Discord (rollback semantics, design §16).
    """
    backup_dir = Path(backup_dir)
    manifest = load_manifest(backup_dir)
    mismatches = _verify_backup_bytes(backup_dir, manifest)
    if mismatches:
        raise RuntimeError(
            "Záloha je poškozená, restore se odmítá: " + "; ".join(mismatches)
        )

    data = Path(data_dir or storage.DATA_DIR).resolve()
    report = {
        "backup_dir": str(backup_dir),
        "created_at_utc": manifest["created_at_utc"],
        "files": len(manifest["files"]),
        "verify_only": verify_only,
    }
    if verify_only:
        return report

    data.mkdir(parents=True, exist_ok=True)
    written = []
    for entry in manifest["files"]:
        src = backup_dir / entry["name"]
        dst = data / entry["name"]
        shutil.copy2(src, dst)
        actual = _sha256_bytes(dst.read_bytes())
        if actual != entry["sha256"]:
            raise RuntimeError(
                f"Restore selhal ověřením u {entry['name']} (soubor zůstal zapsaný)."
            )
        written.append(entry["name"])
    report["restored"] = written
    return report


async def record_backup_markers(
    session_factory,
    *,
    entity_id: str,
    manifest: dict,
) -> None:
    """Persist ``audit_logs`` + ``bot_config`` markers for one backup.

    Never includes connection strings, passwords, or any secret — only counts.
    Caller owns engine lifecycle; runs inside one transaction.
    """
    from db.repositories.sync_audit import AuditRepository, BotConfigRepository
    from db.services.session import transaction

    summary = manifest["summary"]
    details = {
        "backend": manifest.get("backend"),
        "git_rev": manifest.get("git_rev"),
        "alembic_head": manifest.get("alembic_head"),
        "files": summary["files"],
        "bytes": summary["bytes"],
        "records": summary["records"],
        "unreadable": summary["unreadable"],
    }
    async with transaction(session_factory) as session:
        await AuditRepository().append(
            session,
            action="phase_d_backup",
            entity_type="backup",
            entity_id=entity_id,
            details=details,
        )
        await BotConfigRepository().set(
            session,
            "phase_d.backup.latest",
            {"entity_id": entity_id, "details": details},
        )


async def record_restore_markers(
    session_factory,
    *,
    entity_id: str,
    report: dict,
) -> None:
    """Audit marker for one verified restore (design: rollback audit trail)."""
    from db.repositories.sync_audit import AuditRepository
    from db.services.session import transaction

    async with transaction(session_factory) as session:
        await AuditRepository().append(
            session,
            action="phase_d_restore",
            entity_type="backup",
            entity_id=entity_id,
            details={
                "verify_only": report["verify_only"],
                "files": report["files"],
                "restored": report.get("restored", []),
            },
        )
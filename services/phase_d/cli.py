"""Phase D CLI — ``python -m services.phase_d.cli <subcommand>``.

Admin tooling, used manually (not by the bot runtime). DB markers are written
only when the PostgreSQL backend is active; when it is, a marker failure MUST
fail the command loudly (no silent "JSON-only" drift — design §16).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import storage
from db.config import DatabaseConfigError, build_async_database_url

from services.phase_d.backup import (
    backup_data_dir,
    default_backup_root,
    list_backups,
    record_backup_markers,
    record_restore_markers,
    restore_backup,
    utc_stamp,
)
from services.phase_d.inventory import inventory_data_dir, render_markdown


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _make_session_factory(url: str):
    from db.engine import create_async_engine_from_url, make_session_factory

    engine = create_async_engine_from_url(url, poolclass=None)
    return engine, make_session_factory(engine)


async def _write_backup_markers(session_factory, *, entity_id: str, manifest: dict) -> None:
    await record_backup_markers(session_factory, entity_id=entity_id, manifest=manifest)


async def _run_backup_with_markers(entity_id: str, manifest: dict) -> None:
    url = build_async_database_url()
    if not url:
        raise DatabaseConfigError("DATABASE_URL není nastavené – nelze zapsat DB markery.")
    engine, factory = _make_session_factory(url)
    try:
        await _write_backup_markers(factory, entity_id=entity_id, manifest=manifest)
    finally:
        await engine.dispose()


def cmd_backup(args: argparse.Namespace) -> int:
    data_dir = Path(args.data) if args.data else None
    dest_root = Path(args.dest) if args.dest else None
    dest, manifest = backup_data_dir(data_dir, dest_root=dest_root)
    _print_json(manifest)

    if not storage.using_postgres():
        print(f"\n[info] Backend json – DB markery vynechány (záloha: {dest})", file=sys.stderr)
        return 0
    if args.no_db_markers:
        print(f"\n[info] --no-db-markers – audit/bot_config nebyly zapsány (záloha: {dest})", file=sys.stderr)
        return 0
    try:
        asyncio.run(_run_backup_with_markers(dest.name, manifest))
    except Exception as err:
        raise SystemExit(f"Chyba při zápisu DB markerů zálohy (žádný silent fallback): {err}")
    print(f"[ok] DB markery zálohy {dest.name} zapsány (audit_logs + bot_config).", file=sys.stderr)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    data_dir = Path(args.data) if args.data else None
    dest_root = Path(args.dest) if args.dest else None
    if args.latest:
        backups = list_backups(dest_root)
        if not backups:
            raise SystemExit("Žádná záloha k obnovení.")
        backup_dir = Path(backups[0]["dir"])
    else:
        backup_dir = Path(args.backup_dir)
    report = restore_backup(backup_dir, data_dir, verify_only=args.verify_only)
    _print_json(report)

    if storage.using_postgres() and not args.no_db_markers:
        try:
            asyncio.run(_run_restore_markers(backup_dir.name, report))
        except Exception as err:
            raise SystemExit(f"Chyba při zápisu audit markeru restore: {err}")
        print("[ok] Audit marker restore zapsán.", file=sys.stderr)
    return 0


async def _run_restore_markers(entity_id: str, report: dict) -> None:
    url = build_async_database_url()
    engine, factory = _make_session_factory(url)
    try:
        await record_restore_markers(factory, entity_id=entity_id, report=report)
    finally:
        await engine.dispose()


def cmd_list(args: argparse.Namespace) -> int:
    dest_root = Path(args.dest) if args.dest else None
    backups = list_backups(dest_root)
    _print_json({"backups": backups})
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    data_dir = Path(args.data) if args.data else None
    report = inventory_data_dir(data_dir)
    report_dir = Path(args.out) if args.out else default_backup_root() / utc_stamp()
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "inventory_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (report_dir / "inventory_report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    _print_json({"report_dir": str(report_dir), "report": report})
    return 0


async def _run_import(url: str, data_dir: Path) -> None:
    from services.phase_d.import_data import import_json_data

    engine, factory = _make_session_factory(url)
    try:
        report = await import_json_data(factory, data_dir=data_dir)
        _print_json(report)
    finally:
        await engine.dispose()


def cmd_import(args: argparse.Namespace) -> int:
    """D2 — relational import. PostgreSQL-only: JSON fallback is forbidden."""
    if not storage.using_postgres():
        raise SystemExit("Import vyžaduje PostgreSQL backend (JSON fallback zakázán).")
    url = build_async_database_url()
    if not url:
        raise SystemExit("DATABASE_URL není nastavené – import se zastavuje.")
    data_dir = Path(args.data) if args.data else Path(storage.DATA_DIR)
    try:
        asyncio.run(_run_import(url, data_dir))
    except Exception as err:
        raise SystemExit(f"Import selhal (transakce odvolána, nic částečného): {err}")
    print("[ok] Import dokončen v jedné transakci (report výše).", file=sys.stderr)
    return 0


async def _run_unresolved(url: str) -> int:
    from services.phase_d.report_unresolved import build_unresolved_report

    engine, factory = _make_session_factory(url)
    try:
        report = await build_unresolved_report(factory)
        _print_json(report)
        return report["summary"]["open_issues"]
    finally:
        await engine.dispose()


def cmd_unresolved(args: argparse.Namespace) -> int:
    """D3 — unresolved identities: PostgreSQL-only, read-only, no guessing."""
    if not storage.using_postgres():
        raise SystemExit("Report vyžaduje PostgreSQL backend (nerelativní data).")
    url = build_async_database_url()
    if not url:
        raise SystemExit("DATABASE_URL není nastavené – report se zastavuje.")
    try:
        open_issues = asyncio.run(_run_unresolved(url))
    except Exception as err:
        raise SystemExit(f"Report selhal: {err}")
    print(f"[ok] Otevřených issues: {open_issues}.", file=sys.stderr)
    return 0


async def _run_snapshot(url: str, members_path: Path, triggered_by, name) -> None:
    from services.phase_d.snapshot_tiers import load_members, run_snapshot

    engine, factory = _make_session_factory(url)
    try:
        members = load_members(members_path)
        report = await run_snapshot(
            factory,
            members=members,
            triggered_by=triggered_by,
            triggered_by_name=name,
        )
        _print_json(report.as_dict())
    finally:
        await engine.dispose()


def cmd_snapshot(args: argparse.Namespace) -> int:
    """D4 — Discord snapshot: PostgreSQL-only. NEVER mutates Discord roles."""
    if not storage.using_postgres():
        raise SystemExit("Snapshot vyžaduje PostgreSQL backend (mirror úložiště).")
    url = build_async_database_url()
    if not url:
        raise SystemExit("DATABASE_URL není nastavené – snapshot se zastavuje.")
    try:
        asyncio.run(
            _run_snapshot(url, Path(args.members), args.triggered_by, args.triggered_by_name)
        )
    except Exception as err:
        raise SystemExit(f"Snapshot selhal: {err}")
    print("[ok] Snapshot zapsán do mirroru (Discord nebyl změněn).", file=sys.stderr)
    return 0


async def _run_validation(url: str, data_dir: Path) -> None:
    from services.phase_d.report_validation import build_validation_report

    engine, factory = _make_session_factory(url)
    try:
        report = await build_validation_report(factory, data_dir=data_dir)
        _print_json(report)
    finally:
        await engine.dispose()


def cmd_validate(args: argparse.Namespace) -> int:
    """D5 — tri-source report: PostgreSQL-only, read-only, Discord wins."""
    if not storage.using_postgres():
        raise SystemExit("Report vyžaduje PostgreSQL backend.")
    url = build_async_database_url()
    if not url:
        raise SystemExit("DATABASE_URL není nastavené – report se zastavuje.")
    data_dir = Path(args.data) if args.data else Path(storage.DATA_DIR)
    try:
        asyncio.run(_run_validation(url, data_dir))
    except Exception as err:
        raise SystemExit(f"Report selhal: {err}")
    print("[ok] Validace dokončena (žádné automatické opravy).", file=sys.stderr)
    return 0


def cmd_audit_writers(args: argparse.Namespace) -> int:
    """D6 — legacy JSON writer audit: static scan, no DB, no writes."""
    from services.phase_d.legacy_writers import build_writers_report

    report = build_writers_report(Path(args.root))
    _print_json(report)
    print(f"[ok] Writerů nalezeno: {report['writers_found']}.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.phase_d.cli",
        description="Phase D migration/cutover tooling (backup, restore, inventory).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="hashed byte-for-byte backup + DB markery")
    p_backup.add_argument("--data", help="data dir (default: storage.DATA_DIR)")
    p_backup.add_argument("--dest", help="backup root (default: backups/phase_d)")
    p_backup.add_argument("--no-db-markers", action="store_true")
    p_backup.set_defaults(func=cmd_backup)

    p_restore = sub.add_parser("restore", help="verified restore zálohy")
    p_restore.add_argument("backup_dir", nargs="?", help="cesta k záloze (nebo --latest)")
    p_restore.add_argument("--latest", action="store_true")
    p_restore.add_argument("--data", help="data dir (default: storage.DATA_DIR)")
    p_restore.add_argument("--dest", help="backup root (default: backups/phase_d)")
    p_restore.add_argument("--verify-only", action="store_true", help="jen ověření, nic nezapisuje")
    p_restore.add_argument("--no-db-markers", action="store_true")
    p_restore.set_defaults(func=cmd_restore)

    p_list = sub.add_parser("list-backups", help="vypíše dostupné zálohy")
    p_list.add_argument("--dest", help="backup root (default: backups/phase_d)")
    p_list.set_defaults(func=cmd_list)

    p_inv = sub.add_parser("inventory", help="migrační inventura + report")
    p_inv.add_argument("--data", help="data dir (default: storage.DATA_DIR)")
    p_inv.add_argument("--out", help="výstupní složka reportu")
    p_inv.set_defaults(func=cmd_inventory)

    sub.add_parser(
        "unresolved",
        help="D3: report nepřiřazených identit (jen PostgreSQL, read-only)",
    ).set_defaults(func=cmd_unresolved)

    p_snap = sub.add_parser(
        "snapshot-tiers",
        help="D4: Discord snapshot → mirror (jen PostgreSQL, Discord se nemění)",
    )
    p_snap.add_argument("--members", required=True, help="JSON list {id, role_ids}")
    p_snap.add_argument("--triggered-by", type=int, default=None)
    p_snap.add_argument("--triggered-by-name", default=None)
    p_snap.set_defaults(func=cmd_snapshot)

    p_val = sub.add_parser(
        "validate-sources",
        help="D5: tri-source validace (Discord vs mirror vs players.json)",
    )
    p_val.add_argument("--data", help="data dir (default: storage.DATA_DIR)")
    p_val.set_defaults(func=cmd_validate)

    p_wr = sub.add_parser(
        "audit-writers",
        help="D6: audit legacy JSON writerů (statická analýza, bez DB)",
    )
    p_wr.add_argument("--root", default=".", help="repo root (default: .)")
    p_wr.set_defaults(func=cmd_audit_writers)

    p_imp = sub.add_parser(
        "import",
        help="D2: idempotentní relační import legacy JSON (jen PostgreSQL)",
    )
    p_imp.add_argument("--data", help="data dir (default: storage.DATA_DIR)")
    p_imp.set_defaults(func=cmd_import)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
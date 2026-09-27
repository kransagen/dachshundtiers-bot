"""Phase B alembic + constraint/index introspection on a FRESH database.

Mirrors conftest helpers so it can run standalone:
  .venv/bin/python -m tests.phase_b_db_verify
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))


from embedded_postgres import get_server  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.conftest import (  # noqa: E402
    ALL_TABLES,
    EMBEDDED_PG_DIR,
    _alembic_config,
    _create_fresh_database,
    _sync_url,
)

EXPECTED_UNIQUE = {
    "players": ("uq_players_discord_id", "uq_players_ign"),
    "kits": ("uq_kits_key", "uq_kits_name"),
    "tier_definitions": ("uq_tier_definitions_code",),
    "kit_roles": ("uq_kit_roles_kit_tier", "uq_kit_roles_role"),
    "player_current_tiers": ("uq_cur_tier_player_kit",),
    "results": ("uq_results_result_key",),
    "tickets": ("uq_tickets_channel_id", "uq_tickets_open_player_kit"),
    "cooldowns": ("uq_cooldowns_waitlist", "uq_cooldowns_kit"),
    "queues": ("uq_queue_active_kit",),
    "queue_entries": ("uq_queue_waiting_player",),
    "evaluations": ("uq_eval_active",),
    "ticket_members": ("uq_ticket_member",),
}

EXPECTED_CHECK_CONSTRAINTS = {
    "players": ("ck_players_source",),
    "player_current_tiers": ("ck_player_current_tiers_source",),
    "cooldowns": ("ck_cooldowns_type",),
    "results": ("ck_results_promotion_status", "ck_results_kind"),
    "tickets": ("ck_tickets_status", "ck_tickets_ticket_type"),
    "sync_actions": ("ck_sync_actions_status",),
    "outbox_events": ("ck_outbox_events_status",),
    "tier_history": ("ck_tier_history_source",),
}


def _introspect(socket_dir: str, dbname: str) -> None:
    from sqlalchemy import create_engine

    url = _sync_url(socket_dir, dbname)
    engine = create_engine(url)
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public'"
                )
            )
        }
        missing = set(ALL_TABLES) - tables
        assert not missing, f"missing tables: {sorted(missing)}"

        unique_idx = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexdef ILIKE '%UNIQUE%'"
                )
            )
        }
        for table, expected in EXPECTED_UNIQUE.items():
            for name in expected:
                assert name in unique_idx, f"{table}: missing unique index {name}"

        checks = {
            (row[0], row[1])
            for row in conn.execute(
                text(
                    "SELECT conrelid::regclass::text, conname "
                    "FROM pg_constraint WHERE contype = 'c'"
                )
            )
        }
        for table, expected in EXPECTED_CHECK_CONSTRAINTS.items():
            for name in expected:
                assert (table, name) in checks, f"{table}: missing check {name}"

        fks = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT conrelid::regclass::text FROM pg_constraint "
                    "WHERE contype = 'f'"
                )
            )
        }
        print(f"tables={len(tables)} unique_idx={len(unique_idx)} "
              f"checks={len(checks)} fk_tables={len(fks)}")
        print(f"idx sample: {sorted(unique_idx)}")


def main() -> int:
    server = get_server(EMBEDDED_PG_DIR, cleanup_mode="stop")
    socket_dir = str(server.get_postmaster_info().socket_dir)
    try:
        name = "pytest_phase_b_verify"
        _create_fresh_database(socket_dir, name)
        url = _sync_url(socket_dir, name)

        from alembic import command

        cfg = _alembic_config(url)
        command.upgrade(cfg, "head")
        print("upgrade head: OK")
        _introspect(socket_dir, name)

        command.downgrade(cfg, "base")
        print("downgrade base: OK")
        command.upgrade(cfg, "head")
        print("re-upgrade head: OK")
        _introspect(socket_dir, name)
        return 0
    finally:
        server.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
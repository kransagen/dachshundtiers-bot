"""add observe mode to sync_runs

Phase C item 2: the new /sync discord flow mirrors Discord roles into
PostgreSQL and must record its run with an 'observe' mode (read-only on
Discord). Extends ck_sync_runs_mode with 'observe'.

Revision ID: a1b2c3d4e5f6
Revises: 63bdbcfda74a
Create Date: 2026-09-25 09:46:00.000000
"""
from __future__ import annotations

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "63bdbcfda74a"
branch_labels = None
depends_on = None

_SYNC_RUNS_MODES = (
    "mode IN ('preview', 'apply', 'automatic', 'rollback', 'observe')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_sync_runs_mode"), "sync_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_sync_runs_mode"), "sync_runs", _SYNC_RUNS_MODES
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_sync_runs_mode"), "sync_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_sync_runs_mode"),
        "sync_runs",
        "mode IN ('preview', 'apply', 'automatic', 'rollback')",
    )
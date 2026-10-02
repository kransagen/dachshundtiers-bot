"""outbox retry backoff, missing FK/filter indexes, basic CHECK constraints

* ``outbox_events.next_attempt_at`` — retry backoff; ``claim_next`` skips
  events whose backoff has not elapsed.
* indexes on ``queue_entries.player_id``, ``tickets.kit_id``,
  ``results.evaluator_id``, ``sync_actions.status`` and ``sync_actions.created_at``.
* ``tournaments.groups_count > 0`` and ``queue_entries.position >= 0`` are
  added ``NOT VALID``: enforced for every new/updated row without failing the
  migration on legacy rows that may already violate them.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-10-02 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_queue_entries_player", "queue_entries", ["player_id"]),
    ("ix_tickets_kit", "tickets", ["kit_id"]),
    ("ix_results_evaluator", "results", ["evaluator_id"]),
    ("ix_sync_actions_status", "sync_actions", ["status"]),
    ("ix_sync_actions_created", "sync_actions", ["created_at"]),
)


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)
    op.execute(
        "ALTER TABLE tournaments ADD CONSTRAINT ck_tournaments_groups_count "
        "CHECK (groups_count > 0) NOT VALID"
    )
    op.execute(
        "ALTER TABLE queue_entries ADD CONSTRAINT ck_queue_entries_position "
        "CHECK (position >= 0) NOT VALID"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_queue_entries_position"), "queue_entries", type_="check")
    op.drop_constraint(op.f("ck_tournaments_groups_count"), "tournaments", type_="check")
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
    op.drop_column("outbox_events", "next_attempt_at")

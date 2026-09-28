"""enforce one ACTIVE ticket membership per (ticket, player)

Why: the previous uniqueness on ``ticket_members (ticket_id, player_id,
added_at)`` lets two concurrent ``/add`` operations insert two rows that are
both ACTIVE (their ``added_at`` timestamps differ), so a player could appear
twice on one ticket's member list. The new partial unique index
``uq_ticket_members_active`` rejects the second ACTIVE row at the database
level - exactly one active membership per (ticket, player) is a DB-enforced
invariant, not an application convention. Rows with ``removed_at IS NOT NULL``
(history) are untouched, so re-adding a previously removed member stays legal.

Migration safety: before creating the index, any pre-existing duplicate
ACTIVE rows are collapsed to the earliest one per (ticket, player) - the
index would otherwise fail to build on a database that already carries the
bug this migration is fixing.

Revision ID: a3b4c5d6e7f8
Revises: f7a8b9c0d1e2
Create Date: 2026-09-28 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a3b4c5d6e7f8"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deduplicate BEFORE the partial unique index exists, but only among
    # ACTIVE rows (removed_at IS NULL): if two concurrent /add operations
    # created two active memberships for the same (ticket, player), keep the
    # earliest (added_at, then id) and delete the duplicate. Historical rows
    # with removed_at IS NOT NULL are never matched on either side of the
    # join, so re-add history is preserved untouched.
    op.execute(
        sa.text(
            """
            DELETE FROM ticket_members tm
            USING ticket_members dup
            WHERE tm.ticket_id = dup.ticket_id
              AND tm.player_id = dup.player_id
              AND tm.removed_at IS NULL
              AND dup.removed_at IS NULL
              AND (
                    tm.added_at > dup.added_at
                    OR (tm.added_at = dup.added_at AND tm.id > dup.id)
                  )
            """
        )
    )
    op.create_index(
        "uq_ticket_members_active",
        "ticket_members",
        ["ticket_id", "player_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_ticket_members_active", table_name="ticket_members")

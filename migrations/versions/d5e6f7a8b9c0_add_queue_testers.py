"""add queue_testers (queue-scoped active testers for /openq, /joinasqueue, /closeq, /leaveq)

Phase F (#10b): ``active_queues.json[kit]["testers"]`` are queue-scoped
(not the global ``testers`` registry) and gate ``/closeq`` (block while >1
tester) plus the panel embed; JSON-only until now → F10 blocker.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-25 16:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "queue_testers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("queue_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"],
            ["queues.id"],
            name=op.f("fk_queue_testers_queue_id_queues"),
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
            name=op.f("fk_queue_testers_player_id_players"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_testers")),
    )
    op.create_index(
        "uq_queue_tester_player",
        "queue_testers",
        ["queue_id", "player_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_queue_tester_player", table_name="queue_testers")
    op.drop_table("queue_testers")
"""add tester_credits (admin manual backfill ledger for /addtest)

Phase F (#4): ``testers_stats.json`` is derived at runtime from ``results``
per the inventory recommendation (docs/PHASE_F1_INVENTORY.md §5.1 item 1).
``/addtest`` credits historical tests NOT backed by real result rows, so a
small ledger ``tester_credits`` (unique per tester+month, amounts accumulate)
preserves that functionality; totals/monthly merge Result counts + credits.

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-09-25 13:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c4d5e6f7a8b9"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tester_credits",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("tester_id", sa.BigInteger(), nullable=False),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tester_id"],
            ["players.id"],
            name=op.f("fk_tester_credits_tester_id_players"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tester_credits")),
    )
    op.create_index(
        "uq_tester_credit_month",
        "tester_credits",
        ["tester_id", "month"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_tester_credit_month", table_name="tester_credits")
    op.drop_table("tester_credits")
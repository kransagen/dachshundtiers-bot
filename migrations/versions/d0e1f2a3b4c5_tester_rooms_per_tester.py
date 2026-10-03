"""one tester room per tester instead of per kit

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-10-03

``kit_tester_rooms`` (PK ``kit_id``) forced a separate room for every kit.
``tester_rooms`` is keyed by the tester's Discord id, so a tester has exactly one
room that serves every kit they pull from.

Existing rows are carried over: one row per ``created_by`` (the most recently
updated room wins). Rows without ``created_by`` have no owner to map to and are
dropped; those testers simply run ``/mktesterroom`` again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tester_rooms",
        sa.Column("tester_discord_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tester_discord_id", name=op.f("pk_tester_rooms")),
        sa.UniqueConstraint("channel_id", name=op.f("uq_tester_rooms_channel_id")),
    )
    op.execute(
        """
        INSERT INTO tester_rooms (tester_discord_id, channel_id, created_at, updated_at)
        SELECT DISTINCT ON (created_by) created_by, channel_id, created_at, updated_at
        FROM kit_tester_rooms
        WHERE created_by IS NOT NULL
        ORDER BY created_by, updated_at DESC
        """
    )
    op.drop_table("kit_tester_rooms")


def downgrade() -> None:
    op.create_table(
        "kit_tester_rooms",
        sa.Column("kit_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["kit_id"], ["kits.id"], name=op.f("fk_kit_tester_rooms_kit_id_kits")
        ),
        sa.PrimaryKeyConstraint("kit_id", name=op.f("pk_kit_tester_rooms")),
        sa.UniqueConstraint("channel_id", name=op.f("uq_kit_tester_rooms_channel_id")),
    )
    op.drop_table("tester_rooms")

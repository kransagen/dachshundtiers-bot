"""add tournaments + tournament_entries and queue_entries.username

Phase F (F2/F3): tournaments.json moves to relational storage per the user's
"fully relational" decision (docs/PHASE_F1_INVENTORY.md §5.1).

- ``tournaments``: one row per tournament (created via /createturnaj, ends
  via deadline or /deleteturnaj). The row lives until /deleteturnaj removes
  it, mirroring the legacy tournaments.json keyed-by-kit semantics, and
  blocks creating a new tournament for the same kit while it exists.
- ``tournament_entries``: signups (player -> tournament); the legacy
  ``participants`` list. Uniqueness per (tournament, player).
- ``queue_entries.username``: legacy queue entries carried a Discord display
  name that the relational queue_entries table did not store; queue embeds
  render it, so the column is added (nullable, best-effort).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-25 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tournaments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kit_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("groups_count", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column("signup_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("signup_message_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("ended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["kit_id"], ["kits.id"], name=op.f("fk_tournaments_kit_id_kits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tournaments")),
    )
    op.create_index(
        "uq_tournaments_active_kit",
        "tournaments",
        ["kit_id"],
        unique=True,
        postgresql_where=sa.text("ended = false"),
    )

    op.create_table(
        "tournament_entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("tournament_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
            name=op.f("fk_tournament_entries_player_id_players"),
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"],
            ["tournaments.id"],
            name=op.f("fk_tournament_entries_tournament_id_tournaments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tournament_entries")),
    )
    op.create_index(
        "ix_tournament_entries_player", "tournament_entries", ["player_id"], unique=False
    )
    op.create_index(
        "uq_tournament_entry_player",
        "tournament_entries",
        ["tournament_id", "player_id"],
        unique=True,
    )

    op.add_column("queue_entries", sa.Column("username", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("queue_entries", "username")
    op.drop_index("uq_tournament_entry_player", table_name="tournament_entries")
    op.drop_index("ix_tournament_entries_player", table_name="tournament_entries")
    op.drop_table("tournament_entries")
    op.drop_index(
        "uq_tournaments_active_kit",
        table_name="tournaments",
        postgresql_where=sa.text("ended = false"),
    )
    op.drop_table("tournaments")
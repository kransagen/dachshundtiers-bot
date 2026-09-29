"""IGN linking, retire/unretire and peak tiers

* ``players.ign_linked_at`` — when a player confirmed their IGN (/linkign).
  Existing rows stay NULL: whether an old row's IGN was typed by the player
  or copied from a server nickname cannot be told apart, so everyone links
  once.
* ``tickets.ticket_type`` gains ``unretire``.
* ``player_current_tiers.source`` / ``tier_history.source`` gain ``retire``.
* ``player_peak_tiers`` — the permanent peak tier per (player, kit).

Revision ID: b8c9d0e1f2a3
Revises: a3b4c5d6e7f8
Create Date: 2026-09-30 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b8c9d0e1f2a3"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, condition)


def upgrade() -> None:
    op.add_column(
        "players", sa.Column("ign_linked_at", sa.DateTime(timezone=True), nullable=True)
    )
    _replace_check("tickets", "ticket_type", "ticket_type IN ('eval', 'fight', 'unretire')")
    _replace_check(
        "player_current_tiers",
        "source",
        "source IN ('discord_sync', 'promotion', 'manual', 'retire')",
    )
    _replace_check(
        "tier_history",
        "source",
        "source IN ('promotion', 'discord_sync', 'migration', 'manual', 'rollback', 'retire')",
    )
    op.create_table(
        "player_peak_tiers",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("kit_id", sa.BigInteger(), nullable=False),
        sa.Column("tier_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('days', 'wins', 'retire', 'manual')",
            name=op.f("ck_player_peak_tiers_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["kit_id"], ["kits.id"], name=op.f("fk_player_peak_tiers_kit_id_kits")
        ),
        sa.ForeignKeyConstraint(
            ["player_id"], ["players.id"], name=op.f("fk_player_peak_tiers_player_id_players")
        ),
        sa.ForeignKeyConstraint(
            ["tier_id"], ["tier_definitions.id"],
            name=op.f("fk_player_peak_tiers_tier_id_tier_definitions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_player_peak_tiers")),
        sa.UniqueConstraint("player_id", "kit_id", name="uq_peak_player_kit"),
    )


def downgrade() -> None:
    op.drop_table("player_peak_tiers")
    op.execute("DELETE FROM tier_history WHERE source = 'retire'")
    op.execute("UPDATE player_current_tiers SET source = 'manual' WHERE source = 'retire'")
    op.execute(
        "DELETE FROM ticket_members WHERE ticket_id IN "
        "(SELECT id FROM tickets WHERE ticket_type = 'unretire')"
    )
    op.execute("DELETE FROM tickets WHERE ticket_type = 'unretire'")
    _replace_check(
        "tier_history",
        "source",
        "source IN ('promotion', 'discord_sync', 'migration', 'manual', 'rollback')",
    )
    _replace_check(
        "player_current_tiers", "source", "source IN ('discord_sync', 'promotion', 'manual')"
    )
    _replace_check("tickets", "ticket_type", "ticket_type IN ('eval', 'fight')")
    op.drop_column("players", "ign_linked_at")

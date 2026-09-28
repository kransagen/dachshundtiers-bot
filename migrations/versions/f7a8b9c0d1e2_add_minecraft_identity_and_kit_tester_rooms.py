"""add Minecraft identity (minecraft_accounts, player_link_tokens,
players.minecraft_account_id) and kit_tester_rooms

Why: the bot previously identified players only by a free-text ``ign``
column, which is mutable and case-insensitive, and it had no relational link
between a Discord user and a Minecraft account at all. A Minecraft account is
now a first-class entity keyed by its UUID, and the one-to-one
Discord <-> Minecraft link plus the ``kit -> tester room`` mapping are
relations with database-enforced uniqueness instead of application-level
conventions.

New tables:
* ``minecraft_accounts``  – UUID-keyed identity, format-checked by a CHECK.
* ``player_link_tokens``  – one-time, expiring link proofs; single-use.
* ``kit_tester_rooms``    – the authoritative ``kit -> tester channel`` map
  used by ``/queue pull <kit>``.

Changed table:
* ``players.minecraft_account_id`` – UNIQUE FK to ``minecraft_accounts``,
  which is what enforces one-to-one in the database rather than in code.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-28 10:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def upgrade() -> None:
    op.create_table(
        "minecraft_accounts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("uuid", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            f"uuid ~ '{UUID_PATTERN}'", name=op.f("ck_minecraft_accounts_uuid_format")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_minecraft_accounts")),
        sa.UniqueConstraint("uuid", name=op.f("uq_minecraft_accounts_uuid")),
    )

    op.create_table(
        "player_link_tokens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("minecraft_account_id", sa.BigInteger(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "expires_at > issued_at", name=op.f("ck_player_link_tokens_expiry_after_issue")
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND rejection_reason IS NULL)"
            " OR (consumed_at IS NOT NULL AND rejection_reason IS NULL"
            " AND minecraft_account_id IS NOT NULL)"
            " OR (consumed_at IS NOT NULL AND rejection_reason IS NOT NULL)",
            name=op.f("ck_player_link_tokens_consumed_state"),
        ),
        sa.CheckConstraint(
            "rejection_reason IS NULL OR rejection_reason IN "
            "('expired', 'superseded', 'wrong_uuid', 'manual')",
            name=op.f("ck_player_link_tokens_rejection_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["minecraft_account_id"],
            ["minecraft_accounts.id"],
            name=op.f("fk_player_link_tokens_minecraft_account_id_minecraft_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["player_id"],
            ["players.id"],
            name=op.f("fk_player_link_tokens_player_id_players"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_player_link_tokens")),
        sa.UniqueConstraint("code", name="uq_link_token_code"),
    )
    op.create_index(
        "ix_link_token_player_live",
        "player_link_tokens",
        ["player_id"],
        unique=False,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )

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
            ["kit_id"],
            ["kits.id"],
            name=op.f("fk_kit_tester_rooms_kit_id_kits"),
        ),
        sa.PrimaryKeyConstraint("kit_id", name=op.f("pk_kit_tester_rooms")),
        sa.UniqueConstraint("channel_id", name=op.f("uq_kit_tester_rooms_channel_id")),
    )

    op.add_column(
        "players",
        sa.Column("minecraft_account_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_players_minecraft_account_id_minecraft_accounts"),
        "players",
        "minecraft_accounts",
        ["minecraft_account_id"],
        ["id"],
    )
    # One-to-one is enforced here, not in application code: without this
    # constraint two Players could claim the same Minecraft UUID and
    # "who owns this account" would have two answers.
    op.create_unique_constraint(
        op.f("uq_players_minecraft_account_id"), "players", ["minecraft_account_id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_players_minecraft_account_id"), "players")
    op.drop_constraint(
        op.f("fk_players_minecraft_account_id_minecraft_accounts"), "players"
    )
    op.drop_column("players", "minecraft_account_id")

    op.drop_table("kit_tester_rooms")

    op.drop_index("ix_link_token_player_live", table_name="player_link_tokens")
    op.drop_table("player_link_tokens")

    op.drop_table("minecraft_accounts")

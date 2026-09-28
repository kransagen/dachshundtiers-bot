"""Alembic migration + schema introspection tests (embedded PostgreSQL)."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.conftest import (
    ALL_TABLES,
    _alembic_config,
    _create_fresh_database,
    _sync_url,
)


@pytest.fixture(scope="module")
def migration_db_url(embedded_pg):

    _create_fresh_database(embedded_pg, "pytest_migration")
    url = _sync_url(embedded_pg, "pytest_migration")
    yield url
    with create_engine(url).connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        conn.commit()


def _table_names(url: str) -> set:
    with create_engine(url).connect() as conn:
        return set(inspect(conn).get_table_names())


def _current_version(url: str) -> str:
    from sqlalchemy import text

    with create_engine(url).connect() as conn:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    return row[0]


def test_upgrade_creates_all_design_tables(migration_db_url):
    from alembic import command

    command.upgrade(_alembic_config(migration_db_url), "head")
    names = _table_names(migration_db_url)
    assert set(ALL_TABLES) <= names
    assert "alembic_version" in names


def test_migration_version_matches_head(migration_db_url):
    from db.validation import migration_head

    assert _current_version(migration_db_url) == migration_head()


def test_downgrade_is_reversible(migration_db_url):
    from alembic import command

    command.downgrade(_alembic_config(migration_db_url), "base")
    assert _table_names(migration_db_url) == {"alembic_version"}


def test_upgrade_is_idempotent(migration_db_url):
    from alembic import command

    command.upgrade(_alembic_config(migration_db_url), "head")
    command.upgrade(_alembic_config(migration_db_url), "head")
    assert _current_version(migration_db_url) != ""


def test_partial_unique_and_check_constraints(migration_db_url):
    from alembic import command

    command.upgrade(_alembic_config(migration_db_url), "head")
    with create_engine(migration_db_url).connect() as conn:
        inspector = inspect(conn)

        cur_idx = {i["name"]: i for i in inspector.get_indexes("player_current_tiers")}
        assert {"ix_cur_tier_kit", "ix_cur_tier_role"} <= set(cur_idx)
        assert cur_idx["uq_cur_tier_player_kit"]["unique"] is True

        players_idx = {i["name"] for i in inspector.get_indexes("players")}
        assert {"uq_players_discord_id", "uq_players_ign"} <= players_idx

        members_idx = {i["name"]: i for i in inspector.get_indexes("ticket_members")}
        assert members_idx["uq_ticket_members_active"]["unique"] is True

        results_fks = {
            fk["constrained_columns"][0] for fk in inspector.get_foreign_keys("results")
        }
        assert {"player_id", "kit_id"} <= results_fks

        results_checks = {
            c["name"] for c in inspector.get_check_constraints("results")
        }
        assert {"ck_results_kind", "ck_results_promotion_status"} <= results_checks


def test_foreign_keys_present_across_schema(migration_db_url):
    from alembic import command

    command.upgrade(_alembic_config(migration_db_url), "head")
    with create_engine(migration_db_url).connect() as conn:
        inspector = inspect(conn)
        assert inspector.get_foreign_keys("kit_roles")
        assert inspector.get_foreign_keys("sync_actions")
        assert not inspector.get_foreign_keys("bot_config")


def test_model_metadata_matches_migrated_schema(migration_db_url):
    """The schema the migrations build must equal what the models declare.

    This is the guard against *phantom* autogenerate diffs: declaring a
    constraint in a model differently from the object the migration actually
    created (a ``UniqueConstraint`` vs a unique ``Index``, or a fully spelled
    out name that the naming convention would double-prefix) makes every
    future autogenerate run propose dropping and recreating an object that
    never actually changed. Two such bugs existed (``tester_credits`` and
    ``cooldowns``); this test makes the whole schema drift-free at once, so
    any new one fails loudly here instead of in a migration nobody reviewed.
    """
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    import db.models  # noqa: F401  (registers every table on Base.metadata)
    from db.base import Base

    command.upgrade(_alembic_config(migration_db_url), "head")
    with create_engine(migration_db_url).connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"model metadata drifted from the migrated schema: {diff}"


def test_minecraft_identity_constraints(migration_db_url):
    """The Discord <-> Minecraft link is one-to-one *in the database*."""
    from alembic import command

    command.upgrade(_alembic_config(migration_db_url), "head")
    with create_engine(migration_db_url).connect() as conn:
        inspector = inspect(conn)

        # players.minecraft_account_id: UNIQUE + FK -> one-to-one.
        players_uq = {c["name"] for c in inspector.get_unique_constraints("players")}
        players_idx = {i["name"] for i in inspector.get_indexes("players")}
        assert (
            "uq_players_minecraft_account_id" in players_uq
            or "uq_players_minecraft_account_id" in players_idx
        )
        player_fks = {
            fk["constrained_columns"][0] for fk in inspector.get_foreign_keys("players")
        }
        assert "minecraft_account_id" in player_fks

        # A malformed UUID is rejected by a CHECK, not only by Python.
        checks = {c["name"] for c in inspector.get_check_constraints("minecraft_accounts")}
        assert "ck_minecraft_accounts_uuid_format" in checks

        # Link tokens are single-use and expiring.
        token_idx = {i["name"]: i for i in inspector.get_indexes("player_link_tokens")}
        assert "ix_link_token_player_live" in token_idx
        token_uq = {c["name"] for c in inspector.get_unique_constraints("player_link_tokens")}
        assert "uq_link_token_code" in token_uq

        # One tester room per kit; a room belongs to at most one kit.
        room_uq = {c["name"] for c in inspector.get_unique_constraints("kit_tester_rooms")}
        assert "uq_kit_tester_rooms_channel_id" in room_uq
        assert inspector.get_pk_constraint("kit_tester_rooms")["constrained_columns"] == [
            "kit_id"
        ]
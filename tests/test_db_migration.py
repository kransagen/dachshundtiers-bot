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
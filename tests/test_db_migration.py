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

        # One tester room per tester; a room belongs to at most one tester.
        room_uq = {c["name"] for c in inspector.get_unique_constraints("tester_rooms")}
        assert "uq_tester_rooms_channel_id" in room_uq
        assert inspector.get_pk_constraint("tester_rooms")["constrained_columns"] == [
            "tester_discord_id"
        ]
        assert "kit_tester_rooms" not in inspector.get_table_names()


def test_tester_rooms_migration_keeps_one_room_per_tester(embedded_pg):
    """Z roomek po kitech zbyde jedna na testera (nejnovější); bez autora zanikne."""
    from alembic import command

    _create_fresh_database(embedded_pg, "pytest_tester_rooms_mig")
    url = _sync_url(embedded_pg, "pytest_tester_rooms_mig")
    cfg = _alembic_config(url)
    try:
        command.upgrade(cfg, "c9d0e1f2a3b4")
        engine = create_engine(url)
        with engine.begin() as conn:
            for i in (1, 2, 3, 4):
                conn.execute(text("INSERT INTO kits (key, name) VALUES (:k, :k)"), {"k": f"kit{i}"})
            ids = [r[0] for r in conn.execute(text("SELECT id FROM kits ORDER BY id"))]
            rows = [
                (ids[0], 100, 7, "2026-01-01"),
                (ids[1], 101, 7, "2026-02-01"),
                (ids[2], 102, 8, "2026-01-01"),
                (ids[3], 103, None, "2026-01-01"),
            ]
            for kit_id, channel, by, ts in rows:
                conn.execute(
                    text(
                        "INSERT INTO kit_tester_rooms (kit_id, channel_id, created_by, updated_at)"
                        " VALUES (:k, :c, :b, :t)"
                    ),
                    {"k": kit_id, "c": channel, "b": by, "t": ts},
                )
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            got = conn.execute(
                text("SELECT tester_discord_id, channel_id FROM tester_rooms ORDER BY 1")
            ).all()
        assert [tuple(r) for r in got] == [(7, 101), (8, 102)]
        command.downgrade(cfg, "c9d0e1f2a3b4")
        engine.dispose()
    finally:
        with create_engine(url).connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
            conn.commit()


def test_upgrade_to_head_from_older_revision(embedded_pg):
    """Startup auto-migrace: databáze na starší revizi se dostane na head."""
    from alembic import command

    from db.validation import migration_head, upgrade_to_head

    _create_fresh_database(embedded_pg, "pytest_auto_migrate")
    url = _sync_url(embedded_pg, "pytest_auto_migrate")
    try:
        command.upgrade(_alembic_config(url), "e6f7a8b9c0d1")
        before, after = upgrade_to_head(url)
        assert (before, after) == ("e6f7a8b9c0d1", migration_head())
        assert upgrade_to_head(url) == (migration_head(), migration_head())
    finally:
        with create_engine(url).connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
            conn.commit()


def test_upgrade_to_head_on_empty_database(embedded_pg):
    from db.validation import migration_head, upgrade_to_head

    _create_fresh_database(embedded_pg, "pytest_auto_migrate_empty")
    url = _sync_url(embedded_pg, "pytest_auto_migrate_empty")
    assert upgrade_to_head(url) == (None, migration_head())


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("1", True), ("", True), ("0", False), ("false", False), ("OFF", False)],
)
def test_auto_migrate_enabled(monkeypatch, value, expected):
    from db.validation import auto_migrate_enabled

    if value is None:
        monkeypatch.delenv("AUTO_MIGRATE", raising=False)
    else:
        monkeypatch.setenv("AUTO_MIGRATE", value)
    assert auto_migrate_enabled() is expected


def test_upgrade_to_head_escapes_percent_in_url(monkeypatch):
    """URL-encodované heslo (``%40``) nesmí rozbít ConfigParser interpolaci."""
    from alembic import command

    from db import validation

    seen = {}

    def fake_upgrade(cfg, rev):
        seen["url"] = cfg.get_main_option("sqlalchemy.url")

    monkeypatch.setattr(command, "upgrade", fake_upgrade)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *_a, **_k):
            class _R:
                def scalar(self):
                    return None

            return _R()

    class _Engine:
        def connect(self):
            return _Conn()

        def dispose(self):
            pass

    monkeypatch.setattr("sqlalchemy.create_engine", lambda url: _Engine())
    validation.upgrade_to_head("postgresql://u:p%40ss@h/db")
    assert seen["url"] == "postgresql://u:p%40ss@h/db"


def test_migration_chain_has_single_head():
    from alembic.script import ScriptDirectory

    from db.validation import migration_head

    script = ScriptDirectory.from_config(_alembic_config("postgresql://unused/x"))
    assert script.get_heads() == [migration_head()]


def test_backoff_indexes_and_checks_exist_and_enforce(migration_db_url):
    from alembic import command
    from sqlalchemy.exc import IntegrityError

    command.upgrade(_alembic_config(migration_db_url), "head")
    with create_engine(migration_db_url).connect() as conn:
        inspector = inspect(conn)
        assert "next_attempt_at" in {c["name"] for c in inspector.get_columns("outbox_events")}
        assert "ix_queue_entries_player" in {i["name"] for i in inspector.get_indexes("queue_entries")}
        assert "ix_tickets_kit" in {i["name"] for i in inspector.get_indexes("tickets")}
        assert "ix_results_evaluator" in {i["name"] for i in inspector.get_indexes("results")}
        sync_idx = {i["name"] for i in inspector.get_indexes("sync_actions")}
        assert {"ix_sync_actions_status", "ix_sync_actions_created"} <= sync_idx
        assert "ck_tournaments_groups_count" in {
            c["name"] for c in inspector.get_check_constraints("tournaments")
        }
        assert "ck_queue_entries_position" in {
            c["name"] for c in inspector.get_check_constraints("queue_entries")
        }

    with create_engine(migration_db_url).connect() as conn:
        conn.execute(text("INSERT INTO kits (key, name, active) VALUES ('ck', 'CK', true)"))
        try:
            conn.execute(
                text(
                    "INSERT INTO tournaments (kit_id, name, tier, groups_count, category_id, "
                    "signup_channel_id, signup_message_id, role_id, guild_id, deadline) "
                    "SELECT id, 'T', 'LT3', 0, 1, 1, 1, 1, 1, now() FROM kits WHERE key = 'ck'"
                )
            )
            raise AssertionError("groups_count = 0 must be rejected")
        except IntegrityError:
            conn.rollback()


def test_new_migration_downgrades_and_reupgrades(embedded_pg):
    from alembic import command

    _create_fresh_database(embedded_pg, "pytest_migration_c9d0")
    url = _sync_url(embedded_pg, "pytest_migration_c9d0")
    try:
        command.upgrade(_alembic_config(url), "head")
        command.downgrade(_alembic_config(url), "b8c9d0e1f2a3")
        with create_engine(url).connect() as conn:
            inspector = inspect(conn)
            assert "next_attempt_at" not in {
                c["name"] for c in inspector.get_columns("outbox_events")
            }
            assert "ix_tickets_kit" not in {i["name"] for i in inspector.get_indexes("tickets")}
        command.upgrade(_alembic_config(url), "head")
    finally:
        with create_engine(url).connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
            conn.commit()

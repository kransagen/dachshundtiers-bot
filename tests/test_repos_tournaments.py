"""Tournament repository + queue-entries.username schema tests (Phase F F2/F3).

Verifies the relational replacement of ``tournaments.json``:
- one tournament row per kit while active (create is blocked while a row
  exists, mirroring the legacy keyed-by-kit file),
- signups as unique (tournament, player) rows,
- mark_ended / delete semantics,
- alembic migration adds ``tournaments``, ``tournament_entries`` and
  ``queue_entries.username``.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.models import TournamentEntry
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.tournaments import TournamentRepository
from db.services.session import transaction

KITS = (("ht3", "HT3"),)
TIERS = (("t1", "ladder", "Tier 1", 1),)


async def _seed_dimensions(session):
    await ensure_dimensions(session, KITS, TIERS)
    kit = await KitRepository().get_by_key(session, "ht3")
    return kit


async def _seed_player(session, discord_id: int, ign: str):
    _, player = await PlayerRepository().claim_discord_id(
        session, discord_id=discord_id, ign=ign
    )
    return player


async def _seed_tournament(session, kit):
    return await TournamentRepository().create(
        session,
        kit_id=kit.id,
        name="HT3",
        tier="HT3",
        groups_count=2,
        category_id=1001,
        signup_channel_id=1002,
        signup_message_id=1003,
        role_id=1004,
        guild_id=1005,
        deadline=datetime.now(timezone.utc) + timedelta(hours=12),
    )


async def test_signup_add_list_count_and_duplicate_rejected(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        tournament = await _seed_tournament(session, kit)
        alice = await _seed_player(session, 111, "alice")
        bob = await _seed_player(session, 222, "bob")
        repo = TournamentRepository()
        await repo.add_participant(
            session, tournament_id=tournament.id, player_id=alice.id
        )
        await repo.add_participant(
            session, tournament_id=tournament.id, player_id=bob.id
        )
        assert await repo.is_participant(
            session, tournament_id=tournament.id, player_id=alice.id
        )
        assert await repo.list_participant_ids(session, tournament.id) == [
            alice.id,
            bob.id,
        ]
        assert await repo.count_participants(session, tournament.id) == 2
        assert len(await repo.list_all(session)) == 1

        with pytest.raises(IntegrityError):
            await repo.add_participant(
                session, tournament_id=tournament.id, player_id=alice.id
            )


async def test_duplicate_active_for_kit_rejected_and_ended_row_blocks(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        repo = TournamentRepository()
        first = await _seed_tournament(session, kit)
        after_created = await repo.get_by_kit(session, kit_id=kit.id)
        assert after_created.id == first.id

        with pytest.raises(IntegrityError):
            await _seed_tournament(session, kit)

    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        repo = TournamentRepository()
        first = await _seed_tournament(session, kit)
        await repo.mark_ended(session, first.id)
        after_ended = await repo.get_by_kit(session, kit_id=kit.id)
        assert after_ended is not None
        assert after_ended.ended is True


async def test_delete_removes_entries_and_tournament(session_factory, clean_db):
    async with transaction(session_factory) as session:
        kit = await _seed_dimensions(session)
        tournament = await _seed_tournament(session, kit)
        alice = await _seed_player(session, 111, "alice")
        await TournamentRepository().add_participant(
            session, tournament_id=tournament.id, player_id=alice.id
        )
        assert await TournamentRepository().delete(session, tournament.id) is True
        assert await TournamentRepository().get(session, tournament.id) is None
        remaining = (
            await session.execute(
                select(func.count()).select_from(TournamentEntry)
            )
        ).scalar_one()
        assert remaining == 0
        assert await TournamentRepository().get_by_kit(session, kit_id=kit.id) is None


def test_migrations_add_tournament_tables_and_queue_username(migrated_db_url):
    import sqlalchemy as sa

    from alembic import command

    from tests.conftest import _alembic_config

    command.upgrade(_alembic_config(migrated_db_url), "head")
    with sa.create_engine(migrated_db_url).connect() as conn:
        inspector = sa.inspect(conn)
        names = set(inspector.get_table_names())
        assert {"tournaments", "tournament_entries"} <= names
        queue_cols = {c["name"] for c in inspector.get_columns("queue_entries")}
        assert "username" in queue_cols
        tournament_fks = {
            tuple(sorted(fk["constrained_columns"]))
            for fk in inspector.get_foreign_keys("tournament_entries")
        }
        assert ("tournament_id",) in tournament_fks
        assert ("player_id",) in tournament_fks
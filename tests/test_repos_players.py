"""Player repository + identity semantics tests."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.models import Player
from db.repositories.players import (
    CLAIM_ADOPTED,
    CLAIM_CREATED,
    CLAIM_RENAMED,
    CLAIM_UNCHANGED,
    PlayerIdentityError,
    PlayerRepository,
)
from db.services.session import transaction


@pytest.fixture
def repo():
    return PlayerRepository()


async def test_discord_id_lookup_is_stable_key(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        player = await repo.get_or_create_by_discord_id(
            session, discord_id=1001, ign="Alpha"
        )
    async with transaction(session_factory) as session:
        found = await repo.get_by_discord_id(session, 1001)
    assert found is not None
    assert found.id == player.id
    assert found.ign == "Alpha"
    assert found.source == "discord"


async def test_ign_lookup_is_case_insensitive(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        await repo.get_or_create_by_discord_id(session, discord_id=1002, ign="WorKShop")
    async with transaction(session_factory) as session:
        found = await repo.get_by_ign(session, "workshop")
    assert found is not None
    assert found.ign == "WorKShop"


async def test_get_or_create_by_discord_id_rejects_silent_adoption(
    repo, session_factory, clean_db
):
    async with transaction(session_factory) as session:
        await repo.get_or_create_by_ign(session, ign="Ghost")
    async with transaction(session_factory) as session:
        with pytest.raises(PlayerIdentityError):
            await repo.get_or_create_by_discord_id(session, discord_id=1003, ign="Ghost")


async def test_claim_created_then_unchanged(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        outcome, player = await repo.claim_discord_id(
            session, discord_id=2001, ign="Fresh"
        )
    assert outcome == CLAIM_CREATED
    async with transaction(session_factory) as session:
        outcome, _ = await repo.claim_discord_id(session, discord_id=2001, ign="Fresh")
    assert outcome == CLAIM_UNCHANGED


async def test_claim_renamed_keeps_row(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        _, player = await repo.claim_discord_id(session, discord_id=2002, ign="OldName")
    async with transaction(session_factory) as session:
        outcome, renamed = await repo.claim_discord_id(
            session, discord_id=2002, ign="NewName"
        )
    assert outcome == CLAIM_RENAMED
    assert renamed.id == player.id
    assert renamed.ign == "NewName"


async def test_claim_adopts_unclaimed_row_in_place(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        legacy = await repo.get_or_create_by_ign(session, ign="Migrated")
    async with transaction(session_factory) as session:
        outcome, adopted = await repo.claim_discord_id(
            session, discord_id=2003, ign="Migrated"
        )
    assert outcome == CLAIM_ADOPTED
    assert adopted.id == legacy.id


async def test_claim_conflict_never_merges(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        await repo.claim_discord_id(session, discord_id=2004, ign="Taken")
    async with transaction(session_factory) as session:
        with pytest.raises(PlayerIdentityError):
            await repo.claim_discord_id(session, discord_id=2005, ign="Taken")


async def test_rename_conflict_rejected(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        await repo.claim_discord_id(session, discord_id=2006, ign="A")
        _, other = await repo.claim_discord_id(session, discord_id=2007, ign="B")
    async with transaction(session_factory) as session:
        with pytest.raises(PlayerIdentityError):
            await repo.rename_ign(session, player_id=other.id, new_ign="A")


async def test_duplicate_discord_id_violates_unique_index(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        await repo.claim_discord_id(session, discord_id=3000, ign="One")
    async with session_factory() as session:
        session.add(Player(discord_id=3000, ign="Two"))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_duplicate_ign_case_insensitive_violates_unique_index(
    repo, session_factory, clean_db
):
    async with transaction(session_factory) as session:
        await repo.claim_discord_id(session, discord_id=3001, ign="Duplicate")
    async with session_factory() as session:
        session.add(Player(discord_id=3002, ign="DUPLICATE"))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_resolve_prefers_discord_id_over_ign(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        await repo.claim_discord_id(session, discord_id=4000, ign="PlayerOne")
    async with transaction(session_factory) as session:
        player, source = await repo.resolve(
            session, discord_id=4000, ign="playerone"
        )
    assert source == "discord_id"
    assert player is not None and player.discord_id == 4000


async def test_resolve_miss_returns_empty(repo, session_factory, clean_db):
    async with transaction(session_factory) as session:
        player, source = await repo.resolve(session, ign="DoesNotExist")
    assert player is None
    assert source == ""


async def test_transaction_rolls_back_on_failure(repo, session_factory, clean_db):
    with pytest.raises(PlayerIdentityError):
        async with transaction(session_factory) as session:
            await repo.claim_discord_id(session, discord_id=5000, ign="Tmp")
            # second claim with the same IGN but different Discord ID -> conflict
            await repo.claim_discord_id(session, discord_id=5001, ign="Tmp")
    async with transaction(session_factory) as session:
        count = (
            await session.execute(select(func.count()).select_from(Player))
        ).scalar_one()
    assert count == 0
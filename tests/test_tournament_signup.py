"""Regrese: přihlášení do turnaje musí používat players.id, ne Discord ID."""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from db.models import Player, TournamentEntry
from db.repositories.kits import KitRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.tournaments import TournamentRepository
from db.services.session import transaction
from views import signup_player

KITS = (("ht3", "HT3"),)
TIERS = (("t1", "ladder", "Tier 1", 1),)
DISCORD_ID = 1018169843347882076


async def _seed_tournament(session, *, ended=False):
    await ensure_dimensions(session, KITS, TIERS)
    kit = await KitRepository().get_by_key(session, "ht3")
    tournament = await TournamentRepository().create(
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
    if ended:
        await TournamentRepository().mark_ended(session, tournament.id)
    return tournament


async def _entries(session):
    rows = await session.execute(select(TournamentEntry.player_id))
    return list(rows.scalars())


async def test_signup_stores_players_id_not_discord_id(session_factory, clean_db):
    async with transaction(session_factory) as session:
        tournament = await _seed_tournament(session)
        player = await PlayerRepository().get_or_create_shell(
            session, discord_id=DISCORD_ID
        )
        assert player.id != DISCORD_ID

    async with transaction(session_factory) as session:
        assert await signup_player(session, "ht3", DISCORD_ID) == "ok"

    async with transaction(session_factory) as session:
        assert await _entries(session) == [player.id]
        repo = TournamentRepository()
        assert await repo.list_participant_discord_ids(
            session, tournament.id
        ) == [DISCORD_ID]


async def test_signup_creates_missing_player(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await _seed_tournament(session)

    async with transaction(session_factory) as session:
        assert await signup_player(session, "ht3", DISCORD_ID) == "ok"

    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, DISCORD_ID)
        assert player is not None
        assert await _entries(session) == [player.id]


async def test_signup_twice_is_duplicate(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await _seed_tournament(session)

    async with transaction(session_factory) as session:
        assert await signup_player(session, "ht3", DISCORD_ID) == "ok"
    async with transaction(session_factory) as session:
        assert await signup_player(session, "ht3", DISCORD_ID) == "duplicate"

    async with transaction(session_factory) as session:
        count = await session.execute(select(func.count()).select_from(TournamentEntry))
        assert count.scalar_one() == 1


async def test_signup_closed_or_unknown_kit(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await _seed_tournament(session, ended=True)

    async with transaction(session_factory) as session:
        assert await signup_player(session, "ht3", DISCORD_ID) == "closed"
        assert await signup_player(session, "nokit", DISCORD_ID) == "no_kit"
        players = await session.execute(select(func.count()).select_from(Player))
        assert players.scalar_one() == 0


async def test_concurrent_signups_create_one_entry(session_factory, clean_db):
    async with transaction(session_factory) as session:
        await _seed_tournament(session)

    async def attempt():
        async with transaction(session_factory) as session:
            return await signup_player(session, "ht3", DISCORD_ID)

    results = await asyncio.gather(attempt(), attempt(), attempt())
    assert sorted(results) == ["duplicate", "duplicate", "ok"]

    async with transaction(session_factory) as session:
        assert len(await _entries(session)) == 1
        players = await session.execute(select(func.count()).select_from(Player))
        assert players.scalar_one() == 1


async def test_end_signup_groups_use_discord_ids(session_factory, clean_db):
    from unittest import mock

    from cogs import tournaments

    async with transaction(session_factory) as session:
        await _seed_tournament(session)
    async with transaction(session_factory) as session:
        assert await signup_player(session, "ht3", DISCORD_ID) == "ok"

    channel = mock.MagicMock(send=mock.AsyncMock())
    guild = mock.MagicMock()
    guild.get_channel.return_value = channel
    guild.get_member.return_value = None
    group = mock.MagicMock(send=mock.AsyncMock())
    guild.create_text_channel = mock.AsyncMock(return_value=group)

    await tournaments.end_tournament_signup(session_factory, guild, "ht3")

    guild.get_member.assert_called_once_with(DISCORD_ID)
    assert f"<@{DISCORD_ID}>" in group.send.await_args.kwargs["content"]

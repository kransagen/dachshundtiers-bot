"""Feature tests: kit -> tester room, queue pull by kit, Minecraft linking,
eval -> HT3, and HT3 auto IGN/tier.

These are the pieces the brief added on top of the existing relational core.
Each test states the *business rule* it defends, so a future change that keeps
the code passing but breaks the rule shows up as a readable failure.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import services.queue_service as queue_service
from db.base import utcnow
from db.models import (
    Cooldown,
    Kit,
    KitTesterRoom,
    MinecraftAccount,
    Player,
    PlayerLinkToken,
    QueueEntry,
    Result,
    Ticket,
    TierDefinition,
    TierHistory,
    normalize_uuid,
)
from db.repositories.evaluations import EvaluationRepository
from db.repositories.evaluations import TesterRepository as _TesterRepo  # noqa: F401
from db.repositories.identity import PlayerAlreadyLinked, PlayerIdentityRepository
from db.repositories.kits import KitRepository, KitTesterRoomRepository
from db.repositories.queues import (
    QUEUE_ENTRY_PULLED,
    QUEUE_ENTRY_WAITING,
    QueueEntryRepository,
    QueueRepository,
)
from services.ht3_tickets import (
    REFUSE_ALREADY_OPEN,
    REFUSE_NO_EVAL,
    REFUSE_NO_MINECRAFT,
    REFUSE_NO_TIER,
    ensure_eval_ticket,
    resolve_ht3_context,
)
from services.minecraft_link import (
    LinkError,
    complete_link,
    link_status,
    mask_uuid,
    start_link,
    unlink,
)

pytestmark = pytest.mark.asyncio

UUID_A = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
UUID_B = "61699b2e-d327-4a01-9f71-eda9d3b1bc38"
UUID_C = "1c7d0b0a-9a5b-4c3f-8b8b-2a6f1b1f9d3e"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _kit(session, key: str = "boxing", name: str = "Boxing") -> Kit:
    return await KitRepository().get_or_create(session, key=key, name=name)


async def _player(session, *, discord_id: int, ign: str) -> Player:
    return await PlayerRepository().get_or_create_by_discord_id(
        session, discord_id=discord_id, ign=ign
    )


async def _tier(session, code: str) -> TierDefinition:
    return await TierDefinitionRepository().get_or_create(
        session, code=code, kind="ladder", display_name=code
    )


async def _set_current_tier(session, *, player_id: int, kit_id: int, code: str):
    """Put a tier in the Discord-confirmed mirror (the only tier authority)."""
    tier = await _tier(session, code)
    await MirrorServiceRepository().apply_observation(
        session,
        player_id=player_id,
        kit_id=kit_id,
        tier_id=tier.id,
        source="discord_sync",
        observed_at=utcnow(),
    )
    return tier


async def _open_queue(session, *, kit: Kit, players: list[Player]) -> int:
    queue = await QueueRepository().open(session, kit_id=kit.id, name=kit.name)
    for index, player in enumerate(players):
        session.add(
            QueueEntry(
                queue_id=queue.id,
                player_id=player.id,
                kit_id=kit.id,
                ign=player.ign,
                position=index,
                status=QUEUE_ENTRY_WAITING,
                joined_at=utcnow(),
            )
        )
    await session.flush()
    return queue.id


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# Late imports so the module-level import list above stays readable.
from db.repositories.kits import TierDefinitionRepository  # noqa: E402
from db.repositories.players import PlayerRepository  # noqa: E402
from db.repositories.tiers import MirrorServiceRepository  # noqa: E402
from db.repositories.tickets import TicketRepository  # noqa: E402


# ---------------------------------------------------------------------------
# kit -> tester room
# ---------------------------------------------------------------------------


class TestKitTesterRoom:
    async def test_set_and_resolve_roundtrip(self, session_factory, clean_db):
        async with session_factory() as s:
            async with s.begin():
                await _kit(s)

        kit_id = await queue_service.set_tester_room(
            "boxing", 4242, created_by=7, session_factory=session_factory
        )
        assert kit_id is not None
        assert (
            await queue_service.resolve_tester_room("boxing", session_factory=session_factory)
            == 4242
        )

    async def test_unknown_kit_is_refused(self, session_factory, clean_db):
        assert (
            await queue_service.set_tester_room("nope", 1, session_factory=session_factory)
            is None
        )
        assert (
            await queue_service.resolve_tester_room("nope", session_factory=session_factory)
            is None
        )

    async def test_setting_again_replaces_rather_than_duplicates(
        self, session_factory, clean_db
    ):
        """One room per kit: re-running /mktesterroom updates, never duplicates."""
        async with session_factory() as s:
            async with s.begin():
                await _kit(s)

        await queue_service.set_tester_room("boxing", 1, session_factory=session_factory)
        await queue_service.set_tester_room("boxing", 2, session_factory=session_factory)

        async with session_factory() as s:
            rows = list((await s.execute(select(KitTesterRoom))).scalars())
        assert len(rows) == 1
        assert rows[0].channel_id == 2

    async def test_one_channel_cannot_belong_to_two_kits(self, session_factory, clean_db):
        """UNIQUE on channel_id: otherwise /queue pull would be ambiguous."""
        async with session_factory() as s:
            async with s.begin():
                await _kit(s, "boxing", "Boxing")
                await _kit(s, "anchorpvp", "AnchorPvP")

        await queue_service.set_tester_room("boxing", 555, session_factory=session_factory)
        with pytest.raises(IntegrityError):
            await queue_service.set_tester_room(
                "anchorpvp", 555, session_factory=session_factory
            )

    async def test_clear_removes_the_mapping(self, session_factory, clean_db):
        async with session_factory() as s:
            async with s.begin():
                await _kit(s)
        await queue_service.set_tester_room("boxing", 1, session_factory=session_factory)
        assert await queue_service.clear_tester_room("boxing", session_factory=session_factory)
        assert (
            await queue_service.resolve_tester_room("boxing", session_factory=session_factory)
            is None
        )

    async def test_lookup_by_channel_finds_the_kit(self, session_factory, clean_db):
        async with session_factory() as s:
            async with s.begin():
                await _kit(s)
        await queue_service.set_tester_room("boxing", 808, session_factory=session_factory)
        async with session_factory() as s:
            row = await KitTesterRoomRepository().get_for_channel(s, channel_id=808)
        assert row is not None
        assert row.channel_id == 808

    async def test_display_name_also_resolves(self, session_factory, clean_db):
        """Testers paste display names as often as keys."""
        async with session_factory() as s:
            async with s.begin():
                await _kit(s, "anchorpvp", "AnchorPvP")
        await queue_service.set_tester_room(
            "AnchorPvP", 9, session_factory=session_factory
        )
        assert (
            await queue_service.resolve_tester_room("anchorpvp", session_factory=session_factory)
            == 9
        )


# ---------------------------------------------------------------------------
# /queue pull <kit>
# ---------------------------------------------------------------------------


class TestPullByKit:
    async def _seed(self, session_factory, *, room: int | None, count: int = 3):
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                players = [
                    await _player(s, discord_id=100 + i, ign=f"player{i}")
                    for i in range(count)
                ]
                await _open_queue(s, kit=kit, players=players)
        if room is not None:
            await queue_service.set_tester_room(
                "boxing", room, session_factory=session_factory
            )

    async def test_pull_returns_the_first_player_and_the_kits_room(
        self, session_factory, clean_db
    ):
        await self._seed(session_factory, room=4242)
        result = await queue_service.pull_for_kit("boxing", session_factory=session_factory)
        assert result.ok
        assert result.channel_id == 4242
        assert result.player["ign"] == "player0"

    async def test_pull_is_fifo(self, session_factory, clean_db):
        await self._seed(session_factory, room=1)
        pulled = [
            (await queue_service.pull_for_kit("boxing", session_factory=session_factory)).player[
                "ign"
            ]
            for _ in range(3)
        ]
        assert pulled == ["player0", "player1", "player2"]

    async def test_pull_marks_the_entry_pulled_in_the_db(
        self, session_factory, clean_db
    ):
        await self._seed(session_factory, room=1)
        await queue_service.pull_for_kit("boxing", session_factory=session_factory)
        async with session_factory() as s:
            statuses = [
                e.status
                for e in (
                    await s.execute(
                        select(QueueEntry).order_by(QueueEntry.position)
                    )
                ).scalars()
            ]
        assert statuses == [QUEUE_ENTRY_PULLED, QUEUE_ENTRY_WAITING, QUEUE_ENTRY_WAITING]

    async def test_missing_room_keeps_the_player_in_the_queue(
        self, session_factory, clean_db
    ):
        """The important one: no room must never mean 'player silently lost'."""
        await self._seed(session_factory, room=None)
        result = await queue_service.pull_for_kit("boxing", session_factory=session_factory)
        assert result.status == queue_service.PULL_NO_ROOM
        assert result.player is None
        async with session_factory() as s:
            statuses = [
                e.status
                for e in (
                    await s.execute(
                        select(QueueEntry).order_by(QueueEntry.position)
                    )
                ).scalars()
            ]
        assert statuses == [QUEUE_ENTRY_WAITING] * 3

    async def test_empty_queue(self, session_factory, clean_db):
        async with session_factory() as s:
            async with s.begin():
                await _kit(s)
        await queue_service.set_tester_room("boxing", 1, session_factory=session_factory)
        result = await queue_service.pull_for_kit("boxing", session_factory=session_factory)
        assert result.status == queue_service.PULL_EMPTY

    async def test_unknown_kit(self, session_factory, clean_db):
        result = await queue_service.pull_for_kit("nope", session_factory=session_factory)
        assert result.status == queue_service.PULL_NO_KIT

    async def test_concurrent_pulls_never_hand_out_the_same_player_twice(
        self, session_factory, clean_db
    ):
        """FOR UPDATE SKIP LOCKED: the old read-then-update path did double-pull."""
        await self._seed(session_factory, room=1, count=4)
        results = await asyncio.gather(
            *[
                queue_service.pull_for_kit("boxing", session_factory=session_factory)
                for _ in range(4)
            ]
        )
        assert all(r.ok for r in results)
        igns = [r.player["ign"] for r in results]
        assert sorted(igns) == ["player0", "player1", "player2", "player3"]

    async def test_a_left_player_is_never_pulled(self, session_factory, clean_db):
        """status='waiting' is the predicate; a stale read must not resurrect them."""
        await self._seed(session_factory, room=1, count=2)
        async with session_factory() as s:
            async with s.begin():
                entry = (await s.execute(select(QueueEntry))).scalars().first()
                await QueueEntryRepository().transition(
                    s, entry_id=entry.id, status="left"
                )

        result = await queue_service.pull_for_kit("boxing", session_factory=session_factory)
        assert result.ok
        assert result.player["ign"] == "player1"

    async def test_room_belongs_to_exactly_one_pull_result(self, session_factory, clean_db):
        await self._seed(session_factory, room=77)
        result = await queue_service.pull_for_kit("boxing", session_factory=session_factory)
        assert result.channel_id == 77
        async with session_factory() as s:
            entry = (await s.execute(select(QueueEntry))).scalars().first()
            assert entry.room_channel_id is None or entry.room_channel_id == 77


# ---------------------------------------------------------------------------
# Minecraft linking
# ---------------------------------------------------------------------------


class TestMinecraftLink:
    async def _player(self, session_factory, *, discord_id=500, ign="Steve"):
        async with session_factory() as s:
            async with s.begin():
                return await _player(s, discord_id=discord_id, ign=ign)

    async def _backdate_token(self, session_factory, code: str) -> None:
        """Age a token past its expiry.

        Expiry is wall-clock, so the honest way to test it is to move time
        forward, not to ask the database to store a window that never existed
        (`expires_at > issued_at` is a CHECK, correctly).
        """
        async with session_factory() as s:
            async with s.begin():
                token = (
                    await s.execute(
                        select(PlayerLinkToken).where(PlayerLinkToken.code == code)
                    )
                ).scalar_one()
                now = utcnow()
                token.issued_at = now - timedelta(hours=2)
                token.expires_at = now - timedelta(hours=1)

    async def test_normalize_uuid_accepts_both_forms(self):
        undashed = "069a79f444e94726a5befca90e38aaf5"
        assert normalize_uuid(undashed) == UUID_A
        assert normalize_uuid(UUID_A.upper()) == UUID_A
        assert normalize_uuid("") == ""
        assert normalize_uuid("not-a-uuid") == ""

    async def test_start_link_issues_a_code(self, session_factory, clean_db):
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        assert len(code) == 9
        # No ambiguous glyphs a player could mistype.
        assert not (set(code) & set("01OI"))

    async def test_start_link_supersedes_the_previous_code(self, session_factory, clean_db):
        await self._player(session_factory)
        first = await start_link(500, session_factory=session_factory)
        second = await start_link(500, session_factory=session_factory)
        assert first != second

        # The old code is dead: consuming it must fail.
        with pytest.raises(LinkError) as err:
            await complete_link(first, UUID_A, session_factory=session_factory)
        assert err.value.code == "unknown_code"

    async def test_start_link_unknown_player(self, session_factory, clean_db):
        with pytest.raises(LinkError) as err:
            await start_link(999, session_factory=session_factory)
        assert err.value.code == "unknown_player"

    async def test_complete_link_links_the_uuid(self, session_factory, clean_db):
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        status = await complete_link(code, UUID_A, session_factory=session_factory)
        assert status.linked
        assert status.uuid == UUID_A

        async with session_factory() as s:
            player = await PlayerRepository().get_by_discord_id(s, 500)
            account = await s.get(MinecraftAccount, player.minecraft_account_id)
        assert account.uuid == UUID_A

    async def test_a_code_can_only_be_used_once(self, session_factory, clean_db):
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        await complete_link(code, UUID_A, session_factory=session_factory)
        with pytest.raises(LinkError) as err:
            await complete_link(code, UUID_B, session_factory=session_factory)
        assert err.value.code in ("unknown_code", "code_already_used")

    async def test_a_wrong_uuid_burns_the_code_and_is_recorded(
        self, session_factory, clean_db
    ):
        """A mismatched UUID is a mistake or an attempt to steal an account.

        The code must not stay usable either way, and the attempt has to be
        visible afterwards — that record is the whole point of the token
        history, and it is the thing a rollback would erase.
        """
        await self._player(session_factory, discord_id=500, ign="Steve")
        await self._player(session_factory, discord_id=600, ign="Alex")

        owner_code = await start_link(600, session_factory=session_factory)
        await complete_link(owner_code, UUID_A, session_factory=session_factory)

        thief_code = await start_link(500, session_factory=session_factory)
        with pytest.raises(LinkError) as err:
            await complete_link(thief_code, UUID_A, session_factory=session_factory)
        assert err.value.code == "uuid_taken"

        # The code is dead, not merely rejected this once.
        with pytest.raises(LinkError) as retry:
            await complete_link(
                thief_code, UUID_B, session_factory=session_factory
            )
        assert retry.value.code in ("unknown_code", "code_already_used")

        # And the rejection survived the transaction.
        async with session_factory() as s:
            token = (
                await s.execute(
                    select(PlayerLinkToken).where(PlayerLinkToken.code == thief_code)
                )
            ).scalar_one()
        assert token.rejection_reason == "wrong_uuid"
        assert token.consumed_at is not None
        assert token.minecraft_account_id is None

    async def test_concurrent_completion_links_only_once(self, session_factory, clean_db):
        """Single-use is a conditional UPDATE, not a read-then-write."""
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        results = await asyncio.gather(
            *[complete_link(code, UUID_A, session_factory=session_factory) for _ in range(3)],
            return_exceptions=True,
        )
        successes = [r for r in results if not isinstance(r, LinkError)]
        assert len(successes) == 1, [r for r in results if isinstance(r, LinkError)]

    async def test_non_positive_ttl_is_rejected_before_the_database(
        self, session_factory, clean_db
    ):
        """`expires_at > issued_at` is a DB CHECK; the service must not lean on
        it to reject bad input — the caller needs a reportable reason."""
        await self._player(session_factory)
        with pytest.raises(LinkError) as err:
            await start_link(
                500, session_factory=session_factory, ttl=timedelta(seconds=0)
            )
        assert err.value.code == "invalid_ttl"

    async def test_expired_code_is_rejected_and_marked(self, session_factory, clean_db):
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        await self._backdate_token(session_factory, code)
        with pytest.raises(LinkError) as err:
            await complete_link(code, UUID_A, session_factory=session_factory)
        assert err.value.code == "expired"

        async with session_factory() as s:
            row = (await s.execute(select(PlayerLinkToken))).scalar_one()
        assert row.rejection_reason == "expired"
        assert row.consumed_at is not None
        assert row.minecraft_account_id is None

    async def test_malformed_uuid_is_rejected(self, session_factory, clean_db):
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        with pytest.raises(LinkError) as err:
            await complete_link(code, "clearly-not-a-uuid", session_factory=session_factory)
        assert err.value.code == "invalid_uuid"

    async def test_one_uuid_cannot_be_taken_by_two_players(
        self, session_factory, clean_db
    ):
        await self._player(session_factory, discord_id=500, ign="Steve")
        await self._player(session_factory, discord_id=600, ign="Alex")

        code_a = await start_link(500, session_factory=session_factory)
        await complete_link(code_a, UUID_A, session_factory=session_factory)

        code_b = await start_link(600, session_factory=session_factory)
        with pytest.raises(LinkError) as err:
            await complete_link(code_b, UUID_A, session_factory=session_factory)
        assert err.value.code == "uuid_taken"

        async with session_factory() as s:
            owners = list(
                (await s.execute(select(Player).where(
                    Player.minecraft_account_id.is_not(None)
                ))).scalars()
            )
        assert len(owners) == 1
        assert owners[0].discord_id == 500

    async def test_the_database_refuses_a_second_link_to_one_uuid(
        self, session_factory, clean_db
    ):
        """Belt and braces: even a bug in the service cannot break one-to-one."""
        await self._player(session_factory, discord_id=500, ign="Steve")
        await self._player(session_factory, discord_id=600, ign="Alex")
        async with session_factory() as s:
            async with s.begin():
                account = MinecraftAccount(uuid=UUID_C, name="Notch")
                s.add(account)
                await s.flush()
                first = await _player(s, discord_id=700, ign="Third")
                first.minecraft_account_id = account.id
                await s.flush()
                second = await _player(s, discord_id=800, ign="Fourth")
                with pytest.raises(IntegrityError):
                    second.minecraft_account_id = account.id
                    await s.flush()

    async def test_code_is_case_insensitive(self, session_factory, clean_db):
        """The code is documented as case-insensitive; lowercase input works."""
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        status = await complete_link(
            f"  {code.lower()} ", UUID_A, session_factory=session_factory
        )
        assert status.linked and status.uuid == UUID_A

    async def test_player_linked_elsewhere_is_not_silently_repointed(
        self, session_factory, clean_db
    ):
        """A linked player cannot be moved to another UUID without /unlink."""
        await self._player(session_factory)
        first = await start_link(500, session_factory=session_factory)
        await complete_link(first, UUID_A, session_factory=session_factory)

        second = await start_link(500, session_factory=session_factory)
        with pytest.raises(LinkError) as err:
            await complete_link(second, UUID_B, session_factory=session_factory)
        assert err.value.code == "already_linked"

        status = await link_status(500, session_factory=session_factory)
        assert status.uuid == UUID_A
        with pytest.raises(LinkError) as reuse:
            await complete_link(second, UUID_B, session_factory=session_factory)
        assert reuse.value.code == "unknown_code"

    async def test_repository_link_refuses_a_different_account(
        self, session_factory, clean_db
    ):
        async with session_factory() as s:
            async with s.begin():
                one = MinecraftAccount(uuid=UUID_A, name="One")
                two = MinecraftAccount(uuid=UUID_C, name="Two")
                s.add_all([one, two])
                player = await _player(s, discord_id=900, ign="Linked")
                await s.flush()
                repo = PlayerIdentityRepository()
                await repo.link(s, player_id=player.id, account_id=one.id)
                await repo.link(s, player_id=player.id, account_id=one.id)
                with pytest.raises(PlayerAlreadyLinked):
                    await repo.link(s, player_id=player.id, account_id=two.id)

    async def test_link_status_reflects_state(self, session_factory, clean_db):
        await self._player(session_factory)
        before = await link_status(500, session_factory=session_factory)
        assert not before.linked

        code = await start_link(500, session_factory=session_factory)
        pending = await link_status(500, session_factory=session_factory)
        assert not pending.linked
        assert pending.pending_code_expires_at is not None

        await complete_link(code, UUID_A, session_factory=session_factory)
        after = await link_status(500, session_factory=session_factory)
        assert after.linked
        assert after.uuid == UUID_A
        assert after.pending_code_expires_at is None

    async def test_unlink_clears_the_link_but_keeps_the_account(
        self, session_factory, clean_db
    ):
        """Keeping the row means a re-link needn't re-prove UUID ownership."""
        await self._player(session_factory)
        code = await start_link(500, session_factory=session_factory)
        await complete_link(code, UUID_A, session_factory=session_factory)

        assert await unlink(500, session_factory=session_factory) is True
        status = await link_status(500, session_factory=session_factory)
        assert not status.linked

        async with session_factory() as s:
            assert (await s.execute(select(MinecraftAccount))).scalar_one().uuid == UUID_A
        assert await unlink(500, session_factory=session_factory) is False

    async def test_masked_uuid_is_not_reusable(self):
        masked = mask_uuid(UUID_A)
        assert UUID_A not in masked
        assert normalize_uuid(masked) == ""


# ---------------------------------------------------------------------------
# Eval -> HT3 ticket (idempotent)
# ---------------------------------------------------------------------------


class TestEvalToHt3:
    async def _prepare(self, session_factory, *, tier: str = "LT3"):
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=900, ign="Steve")
                await _set_current_tier(
                    s, player_id=player.id, kit_id=kit.id, code=tier
                )
        return kit, player

    async def test_granting_an_eval_asks_for_a_ticket(self, session_factory, clean_db):
        await self._prepare(session_factory)
        request = await ensure_eval_ticket(
            "Steve", "boxing", session_factory=session_factory
        )
        assert request.needs_ticket
        assert request.current_tier == "LT3"
        # LT3 + eval tops out at the HT3 rung.
        assert request.target_tier == "HT3"
        assert request.discord_id == 900

    async def test_repeated_grants_never_ask_twice(self, session_factory, clean_db):
        """The core idempotency guarantee for eval -> HT3."""
        await self._prepare(session_factory)
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=900, ign="Steve")
                await EvaluationRepository().grant(
                    s, player_id=player.id, kit_id=kit.id
                )
                await TicketRepository().open(
                    s,
                    channel_id=4242,
                    player_id=player.id,
                    ign=player.ign,
                    kit_id=kit.id,
                    target_tier_id=(await _tier(s, "HT3")).id,
                    category_id=1,
                    created_at=utcnow(),
                )

        request = await ensure_eval_ticket(
            "Steve", "boxing", session_factory=session_factory
        )
        assert request.needs_ticket is False
        assert request.reason == "already_open"
        assert request.open_ticket["id"] == "4242"

        async with session_factory() as s:
            assert len(list((await s.execute(select(Ticket))).scalars())) == 1

    async def test_two_concurrent_grants_ask_for_a_ticket_only_once(
        self, session_factory, clean_db
    ):
        await self._prepare(session_factory)
        requests = await asyncio.gather(
            *[
                ensure_eval_ticket("Steve", "boxing", session_factory=session_factory)
                for _ in range(4)
            ]
        )
        # Pre-check is advisory; the DB constraint is the authority. Either way
        # the caller must not be told to open a second ticket.
        assert all(r.discord_id == 900 for r in requests)

    async def test_eval_alone_anchors_the_ticket_at_the_eval_rung(
        self, session_factory, clean_db
    ):
        """An eval with no recorded tier still means "beat an LT3 tester".

        ``effective_ticket_tier(None, True)`` returns the virtual ``LT3E``
        precisely so an eval-only player reaches the HT3 rung. That is the
        existing business rule, so /seteval opens the ticket; the *panel*
        path is stricter (it refuses an unrecorded tier) because there the
        player is picking for themselves rather than being evaluated.
        """
        async with session_factory() as s:
            async with s.begin():
                await _kit(s)
                await _player(s, discord_id=900, ign="Steve")
        request = await ensure_eval_ticket(
            "Steve", "boxing", session_factory=session_factory
        )
        assert request.needs_ticket
        assert request.current_tier is None
        assert request.target_tier == "HT3"

    async def test_eval_does_not_lift_a_higher_tier(self, session_factory, clean_db):
        """An eval must not push a strong player back down to the HT3 rung."""
        await self._prepare(session_factory, tier="HT2")
        request = await ensure_eval_ticket(
            "Steve", "boxing", session_factory=session_factory
        )
        assert request.current_tier == "HT2"
        assert request.target_tier == "LT1"

    async def test_unknown_player_or_kit_is_reported_not_guessed(
        self, session_factory, clean_db
    ):
        await self._prepare(session_factory)
        assert (
            await ensure_eval_ticket("Nobody", "boxing", session_factory=session_factory)
        ).reason == "no_player"
        assert (
            await ensure_eval_ticket("Steve", "nope", session_factory=session_factory)
        ).reason == "no_kit"

    async def test_no_database_means_no_ticket(self):
        assert (await ensure_eval_ticket("Steve", "boxing", session_factory=None)).reason == (
            "no_database"
        )


# ---------------------------------------------------------------------------
# HT3 panel: auto IGN + tier
# ---------------------------------------------------------------------------


class TestHT3Context:
    async def _linked_player(self, session_factory, *, tier: str | None = "LT3", eval_ok=True):
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=1100, ign="Steve")
                account = MinecraftAccount(uuid=UUID_A, name="SteveTheReal")
                s.add(account)
                await s.flush()
                player.minecraft_account_id = account.id
                if tier:
                    await _set_current_tier(
                        s, player_id=player.id, kit_id=kit.id, code=tier
                    )
                if eval_ok:
                    await EvaluationRepository().grant(
                        s, player_id=player.id, kit_id=kit.id
                    )
        return kit, player

    async def test_ign_comes_from_the_linked_minecraft_account(
        self, session_factory, clean_db
    ):
        """Not from players.ign – that is why the account name is the truth."""
        await self._linked_player(session_factory)
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert context.ok
        assert context.ign == "SteveTheReal"

    async def test_target_tier_comes_from_the_ladder(self, session_factory, clean_db):
        await self._linked_player(session_factory, tier="LT3")
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert context.current_tier == "LT3"
        assert context.target_tier == "HT3"

    async def test_no_minecraft_link_refuses(self, session_factory, clean_db):
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=1100, ign="Steve")
                await _set_current_tier(s, player_id=player.id, kit_id=kit.id, code="LT3")
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert not context.ok
        assert context.reason == REFUSE_NO_MINECRAFT
        assert "/linkign" in context.message

    async def test_ign_linked_via_linkign_is_enough(self, session_factory, clean_db):
        """/linkign (bez Minecraft pluginu) stačí na HT3+ ticket."""
        from db.base import utcnow as _now

        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=1100, ign="Steve")
                player.ign_linked_at = _now()
                await _set_current_tier(s, player_id=player.id, kit_id=kit.id, code="HT3")
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert context.ok
        assert context.ign == "Steve"

    async def test_no_tier_refuses(self, session_factory, clean_db):
        await self._linked_player(session_factory, tier=None)
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert not context.ok
        assert context.reason == REFUSE_NO_TIER

    async def test_no_eval_and_too_low_a_tier_refuses(self, session_factory, clean_db):
        await self._linked_player(session_factory, tier="LT5", eval_ok=False)
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert not context.ok
        assert context.reason == REFUSE_NO_EVAL

    async def test_an_open_ticket_makes_a_second_one_impossible(
        self, session_factory, clean_db
    ):
        await self._linked_player(session_factory)
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=1100, ign="Steve")
                await TicketRepository().open(
                    s,
                    channel_id=5555,
                    player_id=player.id,
                    ign=player.ign,
                    kit_id=kit.id,
                    target_tier_id=(await _tier(s, "HT3")).id,
                    category_id=1,
                    created_at=utcnow(),
                )
        context = await resolve_ht3_context(
            1100, "boxing", session_factory=session_factory
        )
        assert not context.ok
        assert context.reason == REFUSE_ALREADY_OPEN
        assert context.open_ticket["id"] == "5555"

    async def test_unknown_player_and_kit(self, session_factory, clean_db):
        assert not (
            await resolve_ht3_context(1, "boxing", session_factory=session_factory)
        ).ok
        await self._linked_player(session_factory)
        context = await resolve_ht3_context(
            1100, "nope", session_factory=session_factory
        )
        assert context.reason == "no_kit"

    async def test_no_database_refuses_instead_of_guessing(self):
        context = await resolve_ht3_context(1, "boxing", session_factory=None)
        assert not context.ok


# ---------------------------------------------------------------------------
# Tier history stays relational
# ---------------------------------------------------------------------------


class TierHistoryIsRelationalTests:
    """Tier history is rows with foreign keys, not a list of strings.

    The requirement is that a history entry points at the result/tester/timestamp
    that caused it. These tests check the shape rather than the promotion flow,
    which ``test_phase_e_concurrency`` and the Phase F suite already cover.
    """

    async def test_history_links_to_player_kit_and_both_tiers(
        self, session_factory, clean_db
    ):
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=1500, ign="Steve")
                first = await _tier(s, "LT5")
                second = await _tier(s, "HT5")
                await MirrorServiceRepository().apply_observation(
                    s,
                    player_id=player.id,
                    kit_id=kit.id,
                    tier_id=first.id,
                    source="discord_sync",
                    observed_at=utcnow(),
                )
                await MirrorServiceRepository().apply_observation(
                    s,
                    player_id=player.id,
                    kit_id=kit.id,
                    tier_id=second.id,
                    source="promotion",
                    observed_at=utcnow(),
                )

        async with session_factory() as s:
            rows = list(
                (
                    await s.execute(
                        select(TierHistory).order_by(TierHistory.id)
                    )
                ).scalars()
            )

        assert len(rows) == 2
        # Chained, not a flat set of labels: each row knows where it came from.
        assert rows[0].previous_tier_id is None
        assert rows[0].tier_id == first.id
        assert rows[1].previous_tier_id == first.id
        assert rows[1].tier_id == second.id
        for row in rows:
            assert row.player_id is not None
            assert row.kit_id == kit.id
            assert row.changed_at is not None
            assert row.source in ("discord_sync", "promotion", "manual")

    async def test_history_can_cite_the_result_that_caused_it(
        self, session_factory, clean_db
    ):
        """A promotion must be traceable to the fight that earned it."""
        from db.repositories.tiers import TierHistoryRepository

        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                player = await _player(s, discord_id=1600, ign="Steve")
                tier = await _tier(s, "LT3")
                tester = await _player(s, discord_id=1601, ign="TesterOne")
                await s.flush()
                result = Result(
                    result_key="test-ht3-ticket-1",
                    kind="ticket",
                    player_id=player.id,
                    evaluator_id=tester.id,
                    kit_id=kit.id,
                    new_tier_id=tier.id,
                    outcome="win",
                    recorded_at=utcnow(),
                )
                s.add(result)
                await s.flush()
                await TierHistoryRepository().append(
                    s,
                    player_id=player.id,
                    kit_id=kit.id,
                    tier_id=tier.id,
                    changed_at=utcnow(),
                    source="promotion",
                    result_id=result.id,
                    actor_id=tester.discord_id,
                    actor_name=tester.ign,
                    reason="win vs tester",
                )

        async with session_factory() as s:
            row = (await s.execute(select(TierHistory))).scalar_one()
            linked = await s.get(Result, row.result_id)
            actor = await s.get(Player, row.actor_id)

        assert row.result_id is not None
        assert linked.player_id == row.player_id
        assert linked.evaluator_id is not None
        assert linked.new_tier_id == row.tier_id
        assert row.actor_id == 1601
        assert actor.ign == "TesterOne"
        assert row.reason == "win vs tester"

    async def test_history_is_scoped_per_kit(self, session_factory, clean_db):
        """Two kits are two ladders; a promotion in one must not show in the other."""
        from db.repositories.tiers import TierHistoryRepository

        async with session_factory() as s:
            async with s.begin():
                boxing = await _kit(s, "boxing", "Boxing")
                anchor = await _kit(s, "anchorpvp", "AnchorPvP")
                player = await _player(s, discord_id=1700, ign="Steve")
                tier = await _tier(s, "LT3")
                repo = TierHistoryRepository()
                await repo.append(
                    s,
                    player_id=player.id,
                    kit_id=boxing.id,
                    tier_id=tier.id,
                    changed_at=utcnow(),
                    source="promotion",
                )
                await repo.append(
                    s,
                    player_id=player.id,
                    kit_id=anchor.id,
                    tier_id=tier.id,
                    changed_at=utcnow(),
                    source="promotion",
                )

        async with session_factory() as s:
            repo = TierHistoryRepository()
            for_boxing = await repo.list_for_player(s, player_id=1, kit_id=1)
            everything = await repo.list_for_player(s, player_id=1)

        assert len(for_boxing) == 1
        assert len(everything) == 2


# ---------------------------------------------------------------------------
# Cooldowns stay per (player, kit) – regression guard for the new pull path
# ---------------------------------------------------------------------------


class TestCooldownScopeStillHolds:
    async def test_pulling_does_not_create_a_global_cooldown(
        self, session_factory, clean_db
    ):
        async with session_factory() as s:
            async with s.begin():
                kit = await _kit(s)
                players = [
                    await _player(s, discord_id=200 + i, ign=f"p{i}") for i in range(2)
                ]
                await _open_queue(s, kit=kit, players=players)
        await queue_service.set_tester_room("boxing", 1, session_factory=session_factory)
        await queue_service.pull_for_kit("boxing", session_factory=session_factory)

        async with session_factory() as s:
            rows = list((await s.execute(select(Cooldown))).scalars())
        # No cooldown at all is fine; a global (kit_id IS NULL) one is not.
        assert all(row.kit_id is not None for row in rows)

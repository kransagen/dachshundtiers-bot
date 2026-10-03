"""Phase G0 — H1 cutover proof (canonical promotion flow).

What this file proves, and nothing weaker:

1. The REAL production command callbacks — ``cogs.results.Results.result`` and
   ``cogs.topresult.TopResult.topresult`` — drive the canonical promotion path
   (``db.services.commit_confirmed_promotion``) against a real embedded
   PostgreSQL, and never touch the legacy ``services.store`` JSON transaction
   mechanism while ``db_session_factory`` is configured. That is the H1 defect:
   ``players.json`` participating in the live promotion decision even though
   PostgreSQL was fully configured.
2. The canonical service itself refuses to write a current tier unless Discord
   CONFIRMED the final role state (failure matrix cases 1–8, below).
3. A static (AST/qualname) guard pins the wiring, so silently dropping
   ``session_factory=sf`` or re-introducing a per-cog promotion gate fails the
   build instead of silently regressing to the JSON path.

Discord-side effects (role grants) are mocked — but realistically: ``member.edit``
applies the role set and ``guild.fetch_member`` returns what Discord would
return, because ``auto_grant_kit_role`` now *verifies* the final state.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
import pytest
from sqlalchemy import select

import cogs.results as results_cog
import cogs.topresult as topresult_cog
import services.store as store_module
from cogs.roles import TierRoleGrant
from db.models import Kit, OutboxEvent, PlayerCurrentTier, Result, TierDefinition, TierHistory
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.outbox import (
    OUTBOX_DEAD_LETTER,
    OUTBOX_DONE,
    OUTBOX_IN_PROGRESS,
    OUTBOX_PENDING,
)
from db.repositories.players import PlayerRepository
from db.repositories.results import (
    PROMOTION_COMMITTED,
    PROMOTION_DISCORD_PENDING,
)
from db.services.outbox_consumer import OutboxConsumer
from db.services.promotion import (
    WEDGE_EVENT_TYPE,
    commit_confirmed_promotion,
    grant_confirmation,
)
from db.services.session import transaction as db_transaction

REPO_ROOT = Path(__file__).resolve().parents[1]

DISCORD_ID = 42424242
EVALUATOR_ID = 777
ROLE_ID = 555111
ROLE_ID_LT4 = 555222


# ---------------------------------------------------------------------------
# Mocks: the JSON store must be unreachable while PostgreSQL is configured
# ---------------------------------------------------------------------------
def _forbid_legacy_transaction(*_args, **_kwargs):
    raise AssertionError(
        "services.store.transaction (legacy JSON/JSONB path) must NEVER be "
        "called while db_session_factory is configured — this is exactly "
        "the H1 regression (players.json participating in the live "
        "promotion decision even though PostgreSQL is configured)."
    )


# ---------------------------------------------------------------------------
# Discord doubles
# ---------------------------------------------------------------------------
def _role(rid):
    return SimpleNamespace(id=rid, mention=f"<@&{rid}>", name=f"Role{rid}")


def _make_member(discord_id: int):
    """A member double that behaves like discord.py: ``edit`` APPLIES the
    roles and returns the member as Discord now sees it; ``fetch_member``
    returns a FRESH object. ``roles`` starts empty."""
    member = mock.MagicMock()
    member.id = discord_id
    member.roles = []
    member.voice = None

    def _apply(roles):
        member.roles = list(roles)
        return _snapshot(member)

    member.edit = mock.AsyncMock(side_effect=_apply)
    member.add_roles = mock.AsyncMock()
    member.remove_roles = mock.AsyncMock()
    return member


def _snapshot(member):
    snap = mock.MagicMock()
    snap.id = member.id
    snap.roles = list(member.roles)
    snap.voice = None
    return snap


def _make_guild(member):
    guild = mock.MagicMock()
    guild.get_member.side_effect = (
        lambda mid: member if int(mid) == member.id else None
    )
    guild.fetch_member = mock.AsyncMock(side_effect=lambda _mid: _snapshot(member))
    guild.get_role.side_effect = lambda rid: _role(int(rid))
    guild.channels = []
    guild.fetch_channel = mock.AsyncMock(return_value=None)
    return guild


def _grant(ok=True, *, verified=True, role_id=ROLE_ID, ambiguous=False, note=""):
    return TierRoleGrant(
        ok=ok,
        note=note,
        tier_role_id=role_id,
        ambiguous=ambiguous,
        verified=verified,
    )


async def _seed_dimensions(session_factory):
    """Registered kit ht3, LT3+LT4 ladder tiers, tier→role mappings."""
    async with db_transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (
                ("LT4", "ladder", "LT4", 1),
                ("LT3", "ladder", "LT3", 2),
            ),
        )
        kit = (
            await session.execute(select(Kit).where(Kit.key == "ht3"))
        ).scalar_one()
        for code, role_id in (("LT3", ROLE_ID), ("LT4", ROLE_ID_LT4)):
            tier = (
                await session.execute(
                    select(TierDefinition).where(TierDefinition.code == code)
                )
            ).scalar_one()
            await KitRoleRepository().set_mapping(
                session, kit_id=kit.id, tier_id=tier.id, discord_role_id=role_id
            )
        return kit.id


async def _seed(session_factory):
    """``_seed_dimensions`` plus the player this suite promotes."""
    await _seed_dimensions(session_factory)
    async with db_transaction(session_factory) as session:
        await PlayerRepository().claim_discord_id(
            session, discord_id=DISCORD_ID, ign="Cutover"
        )


def _commit_kwargs(**overrides):
    kwargs = {
        "result_key": "result:queue-1",
        "kind": "queue",
        "discord_id": DISCORD_ID,
        "ign": "Cutover",
        "kit_key": "ht3",
        "new_tier_code": "LT3",
    }
    kwargs.update(overrides)
    return kwargs


async def _current_tiers(session_factory):
    async with db_transaction(session_factory) as session:
        rows = (await session.execute(select(PlayerCurrentTier))).scalars().all()
    return rows


async def _outbox_events(session_factory):
    async with db_transaction(session_factory) as session:
        rows = (
            await session.execute(
                select(OutboxEvent).order_by(OutboxEvent.id)
            )
        ).scalars().all()
    return [
        SimpleNamespace(
            id=r.id,
            status=r.status,
            attempts=r.attempts,
            confirmed=r.discord_role_confirmed,
            payload=r.payload,
        )
        for r in rows
    ]


# ===========================================================================
# 1) The real command callbacks use the canonical DB path
# ===========================================================================
def _results_interaction(member):
    inter = mock.MagicMock()
    inter.user = SimpleNamespace(
        id=EVALUATOR_ID,
        name="tester",
        display_name="tester",
        roles=[SimpleNamespace(id=1, name="Tester")],
        guild_permissions=SimpleNamespace(administrator=False),
    )
    inter.guild = _make_guild(member)
    inter.channel = mock.MagicMock()  # not a TextChannel → queue path, no ticket
    inter.channel_id = 123
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup.send = mock.AsyncMock()
    return inter


def _results_cog(session_factory):
    cog = results_cog.Results.__new__(results_cog.Results)
    cog.bot = mock.MagicMock()
    cog.bot.db_session_factory = session_factory
    cog.bot.get_channel.return_value = None
    return cog


async def test_result_command_commits_canonical_promotion(session_factory, clean_db):
    """`/result` on a queue player: Discord role confirmed → PG result +
    mirror + history committed, players.json store never entered."""
    await _seed(session_factory)
    cog = _results_cog(session_factory)
    member = _make_member(DISCORD_ID)
    inter = _results_interaction(member)

    with (
        mock.patch.object(store_module, "transaction", _forbid_legacy_transaction),
        mock.patch("cogs.results.has_tester_role", return_value=True),
    ):
        await results_cog.Results.result.callback(
            cog,
            interaction=inter,
            hrac=SimpleNamespace(
                id=DISCORD_ID, name="Cutover", display_name="Cutover"
            ),
            ign="Cutover",
            kit="HT3",
            tier="LT3",
            score="3-1",
            outcome="Won",
        )

    # Completed normally (no early-return validation error path).
    inter.response.send_message.assert_not_awaited()
    inter.followup.send.assert_awaited()
    member.edit.assert_awaited_once()

    async with db_transaction(session_factory) as session:
        result_row = (
            await session.execute(
                select(Result).where(Result.result_key.like("result:queue-%"))
            )
        ).scalar_one()
        mirror = (
            await session.execute(select(PlayerCurrentTier))
        ).scalars().all()
        history = (await session.execute(select(TierHistory))).scalars().all()

    assert result_row.promotion_status == PROMOTION_COMMITTED
    assert result_row.kind == "queue"
    assert len(mirror) == 1
    assert mirror[0].source == "promotion"
    assert mirror[0].discord_role_id == ROLE_ID
    assert len(history) == 1
    assert history[0].previous_tier_id is None
    assert (await _outbox_events(session_factory)) == []


async def test_result_in_ht_ticket_reads_db_ticket_not_stale_json(
    session_factory, clean_db
):
    """G0 discovery: without ``session_factory`` ``get_ticket`` only ever
    reads the legacy JSON ticket store, so in DB mode every HT3+ ticket
    evaluation would be misclassified as a plain queue result. Proves the
    command now recognises the real DB ticket."""
    from services.tickets import create_ticket

    await _seed(session_factory)
    created = await create_ticket(
        channel_id=909090,
        owner_id=str(DISCORD_ID),
        owner_name="Cutover",
        ign="Cutover",
        kit="ht3",
        target_tier="LT3",
        current_tier=None,
        eval_ok=False,
        category_id=1,
        now=1_700_000_000_000,
        session_factory=session_factory,
    )
    assert created["result"] == "created"

    cog = _results_cog(session_factory)
    member = _make_member(DISCORD_ID)
    inter = _results_interaction(member)
    inter.channel = mock.MagicMock(spec=discord.TextChannel)
    inter.channel_id = 909090

    with (
        mock.patch.object(store_module, "transaction", _forbid_legacy_transaction),
        mock.patch("cogs.results.has_tester_role", return_value=True),
    ):
        await results_cog.Results.result.callback(
            cog,
            interaction=inter,
            hrac=SimpleNamespace(
                id=DISCORD_ID, name="Cutover", display_name="Cutover"
            ),
            ign="Cutover",
            kit="HT3",
            tier="LT3",
            score="3-1",
            outcome="Won",
        )

    inter.response.send_message.assert_not_awaited()
    member.edit.assert_awaited_once()

    async with db_transaction(session_factory) as session:
        result_row = (
            await session.execute(
                select(Result).where(Result.result_key == "result:909090")
            )
        ).scalar_one()
    assert result_row.promotion_status == PROMOTION_COMMITTED
    assert result_row.ticket_channel_id == 909090
    assert result_row.kind == "ticket"


def _topresult_interaction(member, channel):
    inter = mock.MagicMock()
    inter.user = SimpleNamespace(
        id=EVALUATOR_ID,
        name="tester",
        display_name="tester",
        roles=[SimpleNamespace(id=1, name="Tester")],
        guild_permissions=SimpleNamespace(administrator=False),
    )
    inter.guild = _make_guild(member)
    inter.channel = channel
    inter.channel_id = 555
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup.send = mock.AsyncMock()
    return inter


def _topresult_cog(session_factory, result_channel):
    cog = topresult_cog.TopResult.__new__(topresult_cog.TopResult)
    cog.bot = mock.MagicMock()
    cog.bot.db_session_factory = session_factory
    cog.bot.get_channel.return_value = result_channel
    return cog


@pytest.mark.parametrize("channel", ["text", "other"])
async def test_topresult_command_commits_canonical_promotion(
    session_factory, clean_db, channel
):
    """`/topresult` (tier gained = promotion) commits through the same
    canonical service — and its announcement status is stored in PostgreSQL,
    not in `ht_results.json` (D6)."""
    from services.ht_fights import FightScore

    await _seed(session_factory)
    async with db_transaction(session_factory) as session:
        kit = (await session.execute(select(Kit))).scalars().first()
        kit.first_to = 4
        await PlayerRepository().get_or_create_by_discord_id(
            session, discord_id=DISCORD_ID, ign="Cutover"
        )
        kit_name = kit.name
    ch_id, role_id = 606060, 707070
    if channel == "text":
        chan = mock.MagicMock(spec=discord.TextChannel)
    else:
        chan = mock.MagicMock()
    chan.send = mock.AsyncMock(return_value=SimpleNamespace(id=4242))
    cog = _topresult_cog(session_factory, chan)
    member = _make_member(DISCORD_ID)
    inter = _topresult_interaction(member, chan)

    wizard = topresult_cog.FightWizard(
        cog,
        evaluator=inter.user,
        player_id=str(DISCORD_ID),
        player_name="Cutover",
        ign="Cutover",
        kit_name=kit_name,
        first_to=4,
        current_tier=None,
        target_tier="LT3",
        tier_gained=True,
        # A bridge forces the promotion for a player with no current tier
        # yet (plain tier_gained without a prior tier has no target).
        bridge="LT3",
        ticket=None,
        sections=["LT3"],
        result_channel=chan,
        target_role=SimpleNamespace(id=role_id),
        guild=inter.guild,
        session_factory=session_factory,
    )
    wizard.chosen["LT3"] = ["31337"]
    wizard.names["31337"] = "Rival"
    wizard.scores[("LT3", "31337")] = FightScore(4, 0, False)

    with (
        mock.patch.object(store_module, "transaction", _forbid_legacy_transaction),
        mock.patch("cogs.topresult.has_tester_role", return_value=True),
        mock.patch.object(topresult_cog, "TOP_RESULT_CHANNEL_ID", ch_id),
        mock.patch.object(topresult_cog, "TOP_RESULT_ROLE_ID", role_id),
    ):
        assert await cog.finalize(inter, wizard) is True

    inter.response.send_message.assert_not_awaited()
    inter.followup.send.assert_awaited()
    member.edit.assert_awaited_once()

    async with db_transaction(session_factory) as session:
        result_row = (
            await session.execute(
                select(Result).where(Result.result_key.like("ht_fight:%"))
            )
        ).scalar_one()
        mirror = (
            await session.execute(select(PlayerCurrentTier))
        ).scalars().all()
    assert result_row.promotion_status == PROMOTION_COMMITTED
    assert result_row.kind == "ht_fight"
    # D6 fix: the announcement status must land in PostgreSQL.
    assert result_row.announcement_status == topresult_cog.ANNOUNCEMENT_SENT
    assert result_row.announcement_message_id == 4242
    assert len(mirror) == 1
    assert mirror[0].source == "promotion"
    assert (await _outbox_events(session_factory)) == []


async def test_topresult_retry_view_also_writes_to_postgres(
    session_factory, clean_db
):
    """D6: the retry button's ``set_ht_fight_announcement`` call must reach
    PostgreSQL too — otherwise the retry leaves the row at ``failed`` forever
    while the rest of the command lives in PG."""
    await _seed(session_factory)
    async with db_transaction(session_factory) as session:
        from db.repositories.results import ResultRepository

        await ResultRepository().insert(
            session,
            result_key="ht_fight:retry-1",
            kind="ht_fight",
            player_id=1,
            kit_id=1,
            new_tier_id=1,
        )

    view = topresult_cog.HTFightRetryView(
        result_ids=["retry-1"],
        result_channel=mock.MagicMock(),
        content="msg",
        allowed_mentions=mock.MagicMock(),
        session_factory=session_factory,
    )
    channel = mock.MagicMock()
    channel.send = mock.AsyncMock(return_value=SimpleNamespace(id=9999))
    view.result_channel = channel
    inter = mock.MagicMock()
    inter.user = SimpleNamespace(
        id=EVALUATOR_ID, roles=[SimpleNamespace(id=1, name="Tester")]
    )
    inter.response.send_message = mock.AsyncMock()
    inter.response.edit_message = mock.AsyncMock()

    with (
        mock.patch.object(store_module, "transaction", _forbid_legacy_transaction),
        mock.patch("cogs.topresult.has_tester_role", return_value=True),
    ):
        await view.retry.callback(inter)

    async with db_transaction(session_factory) as session:
        from db.repositories.results import ResultRepository

        updated = await ResultRepository().get_by_key(session, "ht_fight:retry-1")
    assert updated.announcement_status == topresult_cog.ANNOUNCEMENT_SENT
    assert updated.announcement_message_id == 9999


# ===========================================================================
# 2) Failure matrix, cases 1–8
# ===========================================================================
async def test_case1_rejected_discord_mutation_commits_nothing(
    session_factory, clean_db
):
    """Case 1 — Discord definitively refused the mutation (Forbidden /
    missing mapping / member gone). PG must record NO current tier."""
    await _seed(session_factory)
    outcome = await commit_confirmed_promotion(
        session_factory, grant=_grant(ok=False, verified=False), **_commit_kwargs()
    )
    assert outcome.committed is False
    assert outcome.wedged is False
    assert await _current_tiers(session_factory) == []
    async with db_transaction(session_factory) as session:
        assert (await session.execute(select(TierHistory))).scalars().all() == []
        assert (await session.execute(select(Result))).scalars().all() == []
    assert await _outbox_events(session_factory) == []


async def test_case2_no_op_role_already_correct_is_confirmed_and_commits(
    session_factory, clean_db
):
    """Case 2 — the member already holds the target role. Nothing is mutated,
    but the state is confirmed live and DOES commit (otherwise a re-run of
    the same result would never reach PostgreSQL)."""
    await _seed(session_factory)
    member = _make_member(DISCORD_ID)
    guild = _make_guild(member)
    member.roles = [_role(ROLE_ID)]

    with mock.patch("cogs.roles.get_kit_role_map", return_value={"LT3": str(ROLE_ID)}):
        grant = await results_cog.auto_grant_kit_role(guild, str(DISCORD_ID), "ht3", "LT3")
    assert grant.ok and grant.verified
    member.edit.assert_not_awaited()

    outcome = await commit_confirmed_promotion(
        session_factory, grant=grant, **_commit_kwargs()
    )
    assert outcome.committed is True
    mirror = await _current_tiers(session_factory)
    assert [m.discord_role_id for m in mirror] == [ROLE_ID]


async def test_case3_confirmed_success_commits_one_transaction(
    session_factory, clean_db
):
    """Case 3 — Discord confirmed, DB is healthy: result + mirror + history +
    audit land together and idempotently (no duplicate on re-commit)."""
    await _seed(session_factory)
    outcome = await commit_confirmed_promotion(
        session_factory, grant=_grant(), **_commit_kwargs()
    )
    assert outcome.committed is True

    async with db_transaction(session_factory) as session:
        results = (await session.execute(select(Result))).scalars().all()
        history = (await session.execute(select(TierHistory))).scalars().all()
    assert len(results) == 1
    assert results[0].promotion_status == PROMOTION_COMMITTED
    assert len(history) == 1

    again = await commit_confirmed_promotion(
        session_factory, grant=_grant(), **_commit_kwargs()
    )
    assert again.committed is True
    async with db_transaction(session_factory) as session:
        assert len((await session.execute(select(Result))).scalars().all()) == 1
        assert len((await session.execute(select(TierHistory))).scalars().all()) == 1


async def test_case4_pg_commit_failure_wedges_durably(
    session_factory, clean_db
):
    """Case 4 — Discord confirmed, the PG commit transaction fails. Nothing
    is written as current state, the event lands in the outbox as
    ``discord_role_confirmed=True`` and can be replayed later. Discord is
    never reverted."""
    await _seed(session_factory)
    with mock.patch(
        "db.services.promotion.PromotionCommitService."
        "commit_after_discord_success",
        side_effect=RuntimeError("pg down"),
    ):
        outcome = await commit_confirmed_promotion(
            session_factory, grant=_grant(), **_commit_kwargs()
        )
    assert outcome.committed is False
    assert outcome.wedged is True
    assert await _current_tiers(session_factory) == []

    events = await _outbox_events(session_factory)
    assert len(events) == 1
    assert events[0].confirmed is True
    assert events[0].payload["discord_role_id"] == ROLE_ID


async def test_case5_identity_conflict_after_discord_wedges_unresolved(
    session_factory, clean_db
):
    """Case 5 — Discord already mutated, then identity resolution raises
    ``PlayerIdentityError`` (this Discord ID is bound to a different IGN
    than the one the command passed, and that IGN belongs to another player).
    A durable UNRESOLVED wedge (raw identifiers, ``resolved: False``) is
    written — never a guessed FK, never a silent drop."""
    await _seed_dimensions(session_factory)
    async with db_transaction(session_factory) as session:
        players = PlayerRepository()
        # Our Discord ID is bound to a different IGN...
        await players.claim_discord_id(
            session, discord_id=DISCORD_ID, ign="SomebodyElse"
        )
        # ...and the IGN the command passed already belongs to another player.
        await players.claim_discord_id(session, discord_id=999, ign="Cutover")

    from db.repositories.players import PlayerIdentityError

    with pytest.raises(PlayerIdentityError):
        async with db_transaction(session_factory) as session:
            await PlayerRepository().claim_discord_id(
                session, discord_id=DISCORD_ID, ign="Cutover"
            )

    outcome = await commit_confirmed_promotion(
        session_factory, grant=_grant(), **_commit_kwargs()
    )
    assert outcome.committed is False
    assert outcome.wedged is True
    assert await _current_tiers(session_factory) == []

    events = await _outbox_events(session_factory)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["resolved"] is False
    assert payload["discord_id"] == DISCORD_ID
    assert "player_id" not in payload
    # The wedge is genuinely replayable: once a human resolves the identity,
    # the consumer re-resolves the raw identifiers and completes the mirror.
    async with db_transaction(session_factory) as session:
        other = await PlayerRepository().get_by_discord_id(session, 999)
        await session.delete(other)
    from db.services.outbox_consumer import resolve_unresolved_payload_kwargs

    resolved = await resolve_unresolved_payload_kwargs(session_factory, payload)
    assert resolved["result_key"] == "result:queue-1"
    assert resolved["new_tier_id"] is not None
    assert resolved["player_id"] is not None

    consumed = await OutboxConsumer().consume_one(session_factory)
    assert consumed.outcome == "done"
    assert [m.discord_role_id for m in await _current_tiers(session_factory)] == [
        ROLE_ID
    ]


async def _skip_backoff(session_factory):
    from sqlalchemy import update

    from db.models import OutboxEvent

    async with db_transaction(session_factory) as session:
        await session.execute(update(OutboxEvent).values(next_attempt_at=None))


async def test_case5_unresolved_wedge_still_needs_a_human_fix(
    session_factory, clean_db
):
    """The unresolved wedge must not be replayed into authoritative state
    while the identity conflict persists — it retries, then dead-letters."""
    from db.repositories.players import PlayerIdentityError

    await _seed_dimensions(session_factory)
    async with db_transaction(session_factory) as session:
        players = PlayerRepository()
        await players.claim_discord_id(
            session, discord_id=DISCORD_ID, ign="SomebodyElse"
        )
        await players.claim_discord_id(session, discord_id=999, ign="Cutover")

    await commit_confirmed_promotion(
        session_factory, grant=_grant(), **_commit_kwargs()
    )
    assert len(await _outbox_events(session_factory)) == 1

    seen = []
    for _ in range(12):
        await _skip_backoff(session_factory)
        consumed = await OutboxConsumer().consume_one(session_factory)
        if consumed is None:
            break
        seen.append(consumed.outcome)
        events = await _outbox_events(session_factory)
        if events[0].status == OUTBOX_DEAD_LETTER:
            break
    assert OUTBOX_DEAD_LETTER in seen or "dead_letter" in seen
    assert await _current_tiers(session_factory) == []
    with pytest.raises(PlayerIdentityError):
        async with db_transaction(session_factory) as session:
            await PlayerRepository().claim_discord_id(
                session, discord_id=DISCORD_ID, ign="Cutover"
            )


async def test_case6_ambiguous_timeout_nothing_committed(
    session_factory, clean_db
):
    """Case 6 — the Discord call timed out AND the re-read could not confirm
    the state. Genuinely unknown: nothing committed, nothing wedged, and no
    blind inverse mutation of Discord."""
    await _seed(session_factory)
    outcome = await commit_confirmed_promotion(
        session_factory,
        grant=_grant(ok=False, verified=False, ambiguous=True),
        **_commit_kwargs()
    )
    assert outcome.committed is False
    assert outcome.wedged is False
    assert "NEJISTÝ" in outcome.message
    assert await _current_tiers(session_factory) == []
    assert await _outbox_events(session_factory) == []


async def test_case7_ambiguous_timeout_but_applied_commits(
    session_factory, clean_db
):
    """Case 7 — the reverse ambiguity: the request timed out but the re-read
    shows the role really did land. A confirmed change is never reported as
    a failure just because the response was lost."""
    await _seed(session_factory)
    member = _make_member(DISCORD_ID)
    guild = _make_guild(member)
    applied = _snapshot(member)
    applied.roles = [_role(ROLE_ID)]
    member.edit = mock.AsyncMock(side_effect=asyncio.TimeoutError("timed out"))
    guild.fetch_member = mock.AsyncMock(return_value=applied)

    with mock.patch("cogs.roles.get_kit_role_map", return_value={"LT3": str(ROLE_ID)}):
        grant = await results_cog.auto_grant_kit_role(guild, str(DISCORD_ID), "ht3", "LT3")
    assert grant.ok and grant.verified and not grant.ambiguous

    outcome = await commit_confirmed_promotion(
        session_factory, grant=grant, **_commit_kwargs()
    )
    assert outcome.committed is True
    assert [m.discord_role_id for m in await _current_tiers(session_factory)] == [
        ROLE_ID
    ]


async def test_case8_confirmed_mismatch_is_not_committed(
    session_factory, clean_db
):
    """Case 8 — Discord answered with a different role set than requested
    (a concurrent edit won the race). A confirmed non-application must never
    be mirrored, and Discord must not be 'fixed' behind our back."""
    await _seed(session_factory)
    member = _make_member(DISCORD_ID)
    guild = _make_guild(member)
    member.edit = mock.AsyncMock(
        side_effect=lambda roles: mock.MagicMock(roles=[_role(999)])
    )

    with mock.patch("cogs.roles.get_kit_role_map", return_value={"LT3": str(ROLE_ID)}):
        grant = await results_cog.auto_grant_kit_role(guild, str(DISCORD_ID), "ht3", "LT3")
    assert not grant.ok and not grant.verified and not grant.ambiguous
    member.edit.assert_awaited_once()  # and NOT retried / inverted

    outcome = await commit_confirmed_promotion(
        session_factory, grant=grant, **_commit_kwargs()
    )
    assert outcome.committed is False
    assert await _current_tiers(session_factory) == []
    assert await _outbox_events(session_factory) == []


@pytest.mark.parametrize(
    "grant_obj",
    [
        None,
        object(),
        _grant(ok=True, verified=False),  # not verified -> refused
        _grant(ok=False, verified=True),  # refused
        _grant(ok=True, verified=True, role_id=None),  # nothing to mirror
        _grant(ok=True, verified=True, ambiguous=True),  # still needs verified
    ],
)
def test_grant_confirmation_is_fail_closed(grant_obj):
    """The gate is duck-typed and defensive: only a grant that is ok AND
    verified AND carries a role can ever be treated as Discord-confirmed."""
    confirmed, role_id, ambiguous = grant_confirmation(grant_obj)
    assert confirmed is False
    assert ambiguous in (True, False)
    assert confirmed is True or role_id is None or confirmed is False


def test_grant_confirmation_accepts_real_confirmed_grant():
    confirmed, role_id, ambiguous = grant_confirmation(_grant())
    assert confirmed is True
    assert role_id == ROLE_ID
    assert ambiguous is False


async def test_unverified_grant_is_never_committed(session_factory, clean_db):
    await _seed(session_factory)
    outcome = await commit_confirmed_promotion(
        session_factory,
        grant=_grant(ok=True, verified=False),
        **_commit_kwargs()
    )
    assert outcome.committed is False
    assert await _current_tiers(session_factory) == []


async def test_missing_session_factory_writes_nothing(session_factory, clean_db):
    """Legacy no-PostgreSQL deployment: Discord stays the only authority, the
    refusal is loud, and nothing is written or wedged anywhere."""
    await _seed(session_factory)
    outcome = await commit_confirmed_promotion(
        None, grant=_grant(), **_commit_kwargs()
    )
    assert outcome.committed is False
    assert outcome.wedged is False
    assert "PostgreSQL" in outcome.message


# ===========================================================================
# 3) Concurrency: /result and /topresult for the same player at once
# ===========================================================================
async def test_concurrent_same_tier_promotions_deduplicate(
    session_factory, clean_db
):
    """`/result` and `/topresult` racing for the same player + tier must leave
    EXACTLY ONE current-tier row. The second observation of an unchanged
    tier updates the mirror row and writes NO duplicate history (a no-op
    re-observation, not a second promotion)."""
    await _seed(session_factory)
    outcomes = await asyncio.gather(
        commit_confirmed_promotion(
            session_factory,
            grant=_grant(),
            **_commit_kwargs(result_key="result:queue-A"),
        ),
        commit_confirmed_promotion(
            session_factory,
            grant=_grant(),
            **_commit_kwargs(result_key="ht_fight:B"),
        ),
    )
    assert all(o.committed for o in outcomes)

    mirror = await _current_tiers(session_factory)
    assert len(mirror) == 1
    assert mirror[0].discord_role_id == ROLE_ID
    async with db_transaction(session_factory) as session:
        history = (await session.execute(select(TierHistory))).scalars().all()
        results = (await session.execute(select(Result))).scalars().all()
    # Both results are recorded (they are distinct facts), but the tier only
    # changed once, so there is exactly one history row.
    assert len(results) == 2
    assert len(history) == 1
    assert history[0].previous_tier_id is None


async def test_concurrent_different_tier_promotions_keep_history_consistent(
    session_factory, clean_db
):
    """Two promotions to DIFFERENT tiers, concurrently, must not corrupt the
    append-only history: the advisory lock in ``apply_observation`` means the
    loser's ``previous_tier_id`` is the winner's tier, never the same stale
    pre-image twice."""
    await _seed(session_factory)
    outcomes = await asyncio.gather(
        commit_confirmed_promotion(
            session_factory,
            grant=_grant(),
            **_commit_kwargs(result_key="result:queue-A"),
        ),
        commit_confirmed_promotion(
            session_factory,
            grant=_grant(role_id=ROLE_ID_LT4),
            **_commit_kwargs(result_key="ht_fight:B", new_tier_code="LT4"),
        ),
    )
    assert all(o.committed for o in outcomes)

    mirror = await _current_tiers(session_factory)
    assert len(mirror) == 1
    async with db_transaction(session_factory) as session:
        history = (
            await session.execute(select(TierHistory).order_by(TierHistory.id))
        ).scalars().all()
    assert len(history) == 2
    # First row is a first observation (no previous), the second chains off it.
    assert history[0].previous_tier_id is None
    assert history[1].previous_tier_id == history[0].tier_id
    assert mirror[0].tier_id == history[1].tier_id


async def test_concurrent_same_result_key_is_idempotent(session_factory, clean_db):
    """The same result committed twice concurrently must not duplicate."""
    await _seed(session_factory)
    await asyncio.gather(
        commit_confirmed_promotion(
            session_factory, grant=_grant(), **_commit_kwargs()
        ),
        commit_confirmed_promotion(
            session_factory, grant=_grant(), **_commit_kwargs()
        ),
    )
    async with db_transaction(session_factory) as session:
        results = (await session.execute(select(Result))).scalars().all()
        history = (await session.execute(select(TierHistory))).scalars().all()
    assert len(results) == 1
    assert len(history) == 1


# ===========================================================================
# 4) Outbox: replay, stale claim reclaim, dead-letter, unconfirmed refusal
# ===========================================================================
async def test_wedge_is_replayed_into_postgres(session_factory, clean_db):
    """A wedged, Discord-confirmed event converges to authoritative PG state
    on the next consumer pass."""
    await _seed(session_factory)
    with mock.patch(
        "db.services.promotion.PromotionCommitService."
        "commit_after_discord_success",
        side_effect=RuntimeError("pg down"),
    ):
        outcome = await commit_confirmed_promotion(
            session_factory, grant=_grant(), **_commit_kwargs()
        )
    assert outcome.wedged is True
    assert await _current_tiers(session_factory) == []

    consumed = await OutboxConsumer().consume_one(session_factory)
    assert consumed is not None
    assert consumed.outcome == "done"
    assert [m.discord_role_id for m in await _current_tiers(session_factory)] == [
        ROLE_ID
    ]
    events = await _outbox_events(session_factory)
    assert events[0].status == OUTBOX_DONE

    # Replaying the same wedge again is a no-op (idempotent per result_key).
    again = await commit_confirmed_promotion(
        session_factory, grant=_grant(), **_commit_kwargs()
    )
    assert again.committed is True
    async with db_transaction(session_factory) as session:
        history = (await session.execute(select(TierHistory))).scalars().all()
    assert len(history) == 1


async def test_stale_in_progress_claim_is_reclaimed(session_factory, clean_db):
    """A consumer that died mid-replay leaves an in_progress claim; the next
    pass must reclaim it instead of leaving the promotion stuck forever."""
    from datetime import datetime, timedelta, timezone

    from db.repositories.outbox import OutboxRepository

    await _seed(session_factory)
    with mock.patch(
        "db.services.promotion.PromotionCommitService."
        "commit_after_discord_success",
        side_effect=RuntimeError("pg down"),
    ):
        await commit_confirmed_promotion(
            session_factory, grant=_grant(), **_commit_kwargs()
        )

    async with db_transaction(session_factory) as session:
        repo = OutboxRepository()
        event = await repo.claim_next(session, event_type=WEDGE_EVENT_TYPE)
        event_id = event.id
        event.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        event.status = OUTBOX_IN_PROGRESS
        await session.flush()

    # Without the stale cutoff the claim is still held...
    held = await OutboxConsumer().consume_one(session_factory)
    assert held is None
    assert (await _outbox_events(session_factory))[0].status == OUTBOX_IN_PROGRESS

    # ...and is reclaimed once it is older than the stale window.
    reclaimed = await OutboxConsumer().consume_one(
        session_factory, in_progress_before=default_cutoff()
    )
    assert reclaimed is not None
    assert reclaimed.event_id == event_id
    assert reclaimed.outcome == "done"
    assert [m.discord_role_id for m in await _current_tiers(session_factory)] == [
        ROLE_ID
    ]


def default_cutoff():
    from db.services.outbox_consumer import default_stale_cutoff

    return default_stale_cutoff()


async def test_repeated_replay_failure_dead_letters(session_factory, clean_db):
    """A wedge that can never be replayed must end up in the dead-letter
    state (visible in /sync check), never looped forever or dropped."""
    await _seed(session_factory)
    seen = []
    with mock.patch(
        "db.services.promotion.PromotionCommitService."
        "commit_after_discord_success",
        side_effect=RuntimeError("pg down"),
    ):
        await commit_confirmed_promotion(
            session_factory, grant=_grant(), **_commit_kwargs()
        )
        for _ in range(12):
            await _skip_backoff(session_factory)
            consumed = await OutboxConsumer().consume_one(session_factory)
            if consumed is None:
                break
            seen.append(consumed.outcome)
            events = await _outbox_events(session_factory)
            if events[0].status == OUTBOX_DEAD_LETTER:
                break

    assert "dead_letter" in seen
    events = await _outbox_events(session_factory)
    assert events[0].status == OUTBOX_DEAD_LETTER
    assert await _current_tiers(session_factory) == []


async def test_unconfirmed_outbox_event_is_refused(session_factory, clean_db):
    """An event without ``discord_role_confirmed=True`` must never be
    replayed into authoritative PG state (that would be PG -> Discord
    direction leaking in)."""
    from db.repositories.outbox import OutboxRepository

    await _seed(session_factory)
    async with db_transaction(session_factory) as session:
        await OutboxRepository().enqueue(
            session,
            event_type=WEDGE_EVENT_TYPE,
            aggregate_type="result",
            aggregate_id="result:forged",
            payload={"result_key": "result:forged", "version": 1},
            discord_role_confirmed=False,
        )

    consumed = await OutboxConsumer().consume_one(session_factory)
    assert consumed is not None
    assert consumed.outcome in ("refused", "dead_letter")
    assert await _current_tiers(session_factory) == []
    events = await _outbox_events(session_factory)
    assert events[0].status in (OUTBOX_PENDING, OUTBOX_DEAD_LETTER)


# ===========================================================================
# 5) Health: the promotion wedge is actually operator-visible now
# ===========================================================================
async def test_stuck_discord_pending_promotion_is_reported_by_health(
    session_factory, clean_db
):
    """A Result left at ``discord_pending`` (the grant never confirmed) is now
    surfaced by the health report that `/sync check` renders — before G0 it
    was only visible by hand-written SQL."""
    from datetime import datetime, timedelta, timezone

    from db.repositories.results import ResultRepository
    from db.services.health import build_health_report

    await _seed(session_factory)
    async with db_transaction(session_factory) as session:
        kit = (
            await session.execute(select(Kit).where(Kit.key == "ht3"))
        ).scalar_one()
        tier = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "LT3")
            )
        ).scalar_one()
        _outcome, p = await PlayerRepository().claim_discord_id(
            session, discord_id=DISCORD_ID, ign="Cutover"
        )
        await ResultRepository().insert(
            session,
            result_key="result:stuck-1",
            kind="queue",
            player_id=p.id,
            kit_id=kit.id,
            new_tier_id=tier.id,
            promotion_status=PROMOTION_DISCORD_PENDING,
            recorded_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )

    report = await build_health_report(session_factory)
    stuck = next(c for c in report["checks"] if c["key"] == "unresolved_promotions")
    assert stuck["status"] == "degraded"
    assert "1" in stuck["detail"]


async def test_health_report_flags_dead_letter_outbox(session_factory, clean_db):
    from db.repositories.outbox import OutboxRepository
    from db.services.health import build_health_report

    await _seed(session_factory)
    async with db_transaction(session_factory) as session:
        await OutboxRepository().enqueue(
            session,
            event_type=WEDGE_EVENT_TYPE,
            aggregate_type="result",
            aggregate_id="result:dead",
            payload={"version": 1},
            discord_role_confirmed=True,
        )
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.aggregate_id == "result:dead")
            )
        ).scalar_one()
        event.status = OUTBOX_DEAD_LETTER

    report = await build_health_report(session_factory)
    backlog = next(c for c in report["checks"] if c["key"] == "outbox_backlog")
    assert backlog["status"] == "failed"


# ===========================================================================
# 6) Static wiring guard (AST/qualname) — the anti-regression test
# ===========================================================================
def _parse(rel: str) -> ast.Module:
    return ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))


def _calls_inside(tree: ast.Module, qualname: str) -> list[ast.Call]:
    """Every call made directly inside the function with this qualname."""
    found: list[ast.Call] = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def _qual(self) -> str:
            return ".".join(self.stack)

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if self._qual() == qualname:
                found.append(node)
            self.generic_visit(node)

    V().visit(tree)
    return found


def _callee(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return ast.unparse(node.func)
    return ""


@pytest.mark.parametrize(
    "module_rel,qualname,callee",
    [
        ("cogs/results.py", "Results.result", "record_result"),
        ("cogs/topresult.py", "TopResult.finalize", "record_ht_fights"),
    ],
)
def test_command_passes_session_factory(module_rel, qualname, callee):
    """H1 anti-regression: the live commands MUST hand the DB session
    factory to the recording service, so the DB dispatcher is taken first
    and the JSON branch is never reached."""
    tree = _parse(module_rel)
    calls = [c for c in _calls_inside(tree, qualname) if _callee(c) == callee]
    assert calls, f"{qualname} no longer calls {callee}()"
    for call in calls:
        passed = {kw.arg for kw in call.keywords}
        assert "session_factory" in passed, (
            f"{module_rel}:{qualname} calls {callee}() WITHOUT session_factory — "
            "this is the H1 regression (players.json would be written first, "
            "in every deployment, and the PG mirror would only be a later, "
            "separately-gated step)."
        )


@pytest.mark.parametrize("module_rel,function", [
    ("services/results.py", "record_result"),
    ("services/topresult.py", "record_ht_fight"),
    ("services/topresult.py", "record_ht_fights"),
])
def test_dispatcher_has_no_json_branch_anymore(module_rel, function):
    """The legacy JSON branch is GONE — not behind a gate, deleted.

    H1/G0 anti-regression, post-cutover: ``record_result`` /
    ``record_ht_fight`` must (a) require ``session_factory`` (no ``= None``
    default, so a caller that forgets it fails loudly at the call site) and
    (b) contain no ``transaction(`` call at all — there is no JSON file
    write left to gate. Reintroducing any JSON fallback in these functions
    instantly breaks this test and the json_compat allowlist.
    """
    src = (REPO_ROOT / module_rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    )
    body_src = ast.unparse(fn)
    # (a) required session_factory — no ``= None`` / ``| None`` default.
    assert "session_factory=None" not in body_src, (
        f"{module_rel}::{function} still defaults session_factory to None — "
        "a caller could silently pick a (long-deleted) JSON path"
    )
    # (b) no JSON transaction write anywhere in the function.
    assert "transaction(" not in body_src, (
        f"{module_rel}::{function} still contains a JSON transaction write"
    )
    # (c) it must still gate on None explicitly where the signature allows it.
    fn_sig = ast.unparse(fn.args)
    assert "session_factory" in fn_sig


@pytest.mark.parametrize("module_rel,qualname", [
    ("cogs/results.py", "Results.result"),
    ("cogs/topresult.py", "TopResult.finalize"),
])
def test_command_uses_canonical_promotion_service(module_rel, qualname):
    """There must be exactly ONE promotion write path in each command, and it
    is the canonical service — no duplicated per-cog `grant.ok` gate that can
    drift from the service's own (stricter) rule."""
    tree = _parse(module_rel)
    calls = _calls_inside(tree, qualname)
    names = [_callee(c) for c in calls]
    assert names.count("commit_confirmed_promotion") == 1
    assert "commit_promotion_with_wedge" not in names
    # No inline re-implementation of the invariant-6 gate in the command.
    for call in calls:
        if _callee(call) == "commit_confirmed_promotion":
            assert any(kw.arg == "grant" for kw in call.keywords), (
                "the canonical service must receive the raw grant so it can "
                "enforce the verified/confirmed rule itself"
            )


def test_canonical_service_is_exported_and_used():
    """`db.services` is the only import surface the cogs need, and the
    canonical service is the one exported."""
    import db.services as services_pkg

    assert "commit_confirmed_promotion" in services_pkg.__all__
    assert services_pkg.commit_confirmed_promotion is commit_confirmed_promotion
    for module_rel in ("cogs/results.py", "cogs/topresult.py"):
        assert "commit_confirmed_promotion" in (
            REPO_ROOT / module_rel
        ).read_text(encoding="utf-8")


def test_grant_verified_defaults_to_false():
    """Fail-closed default: a new return site in `auto_grant_kit_role` that
    forgets to confirm the final Discord state cannot silently enable a PG
    commit."""
    assert inspect.signature(TierRoleGrant).parameters["verified"].default is False


def test_no_production_caller_reinverts_discord_after_ambiguity():
    """A genuinely unknown Discord outcome must never trigger an inverse
    Discord mutation. `auto_grant_kit_role` is the only tier-role mutation
    point, and it never retries/reverts on the ambiguous path."""
    from cogs.roles import auto_grant_kit_role

    src = inspect.getsource(auto_grant_kit_role)
    assert src.count("member.edit(") == 1, (
        "auto_grant_kit_role must issue exactly ONE Discord mutation — a second "
        "one (an inverse/retry edit) would be PG/JSON -> Discord."
    )


# ===========================================================================
# 7) JSON compatibility: no players.json path can reach a current tier
# ===========================================================================
def test_json_compat_scan_has_no_forbidden_or_unclassified_flows():
    from services.phase_e.json_compat import build_json_compat_report

    report = build_json_compat_report(REPO_ROOT)
    assert report["json_to_discord_violations"] == []
    assert report["json_to_pg_violations"] == []
    flows = {
        json_compat_flow(s) for s in report["tier_readers"] + report["tier_writers"]
    }
    assert "json_first_write_pending_db_gate" not in flows, (
        "the H1 flow label must stay deleted; the anti-regression is enforced "
        "by the AST wiring tests in this file instead."
    )
    assert "unclassified" not in flows


def json_compat_flow(site):
    from services.phase_e.json_compat import classify_tier_reader

    return classify_tier_reader(site)

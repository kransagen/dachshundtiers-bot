"""PromotionCommitService + wedge tests (invariants 6 & 7)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.models import AuditLog, Cooldown, Result, TierHistory, Ticket
from db.repositories.outbox import OUTBOX_DONE, OutboxRepository
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorRepository
from db.services.outbox_consumer import CONSUMED_DONE, OutboxConsumer
from db.services.promotion import (
    CooldownSpec,
    PromotionCommitService,
    PromotionWedgeOutcome,
    commit_promotion_with_wedge,
    enqueue_promotion_wedge,
)
from db.services.session import transaction


async def _seed(session_factory):
    from db.repositories.kits import ensure_dimensions

    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t2 = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "t2")
        )).scalar_one()
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Promo"
        )
        result = {
            "kit": kit,
            "player": player,
            "tier": t2,
        }
        return result


from db.models import Kit, TierDefinition  # noqa: E402


async def test_commit_after_discord_success_full_history(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    now = datetime.now(timezone.utc)
    service = PromotionCommitService()
    outcome = await service.commit_after_discord_success(
        session_factory,
        result_key="promo-1",
        kind="ticket",
        player_id=seeded["player"].id,
        kit_id=seeded["kit"].id,
        new_tier_id=seeded["tier"].id,
        discord_role_id=777777,
        previous_tier_id=None,
        recorded_at=now,
        cooldowns=(
            CooldownSpec(cooldown_type="ht3", expires_at=now + timedelta(days=7),
                         kit_id=seeded["kit"].id),
        ),
        audit_actor_id=42,
        audit_actor_name="Mod",
    )
    assert outcome.already_committed is False
    assert outcome.observation is not None
    async with transaction(session_factory) as session:
        result = await session.get(Result, outcome.result_id)
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        history = (
            await session.execute(
                select(TierHistory).where(TierHistory.player_id == seeded["player"].id)
            )
        ).scalars().all()
        cooldowns = (
            await session.execute(
                select(Cooldown).where(Cooldown.player_id == seeded["player"].id)
            )
        ).scalars().all()
        audits = (
            await session.execute(select(AuditLog))
        ).scalars().all()
    assert result.promotion_status == "committed"
    assert result.new_tier_id == seeded["tier"].id
    assert mirror is not None and mirror.source == "promotion"
    assert len(history) == 1 and history[0].source == "promotion"
    assert len(cooldowns) == 1
    assert len(audits) == 1 and audits[0].action == "promotion_committed"


async def test_commit_is_idempotent_no_duplicate_history(session_factory, clean_db):
    seeded = await _seed(session_factory)
    now = datetime.now(timezone.utc)
    service = PromotionCommitService()
    first = await service.commit_after_discord_success(
        session_factory,
        result_key="promo-2",
        kind="ticket",
        player_id=seeded["player"].id,
        kit_id=seeded["kit"].id,
        new_tier_id=seeded["tier"].id,
        discord_role_id=777778,
        recorded_at=now,
    )
    second = await service.commit_after_discord_success(
        session_factory,
        result_key="promo-2",
        kind="ticket",
        player_id=seeded["player"].id,
        kit_id=seeded["kit"].id,
        new_tier_id=seeded["tier"].id,
        discord_role_id=777778,
        recorded_at=now,
    )
    assert second.already_committed is True
    assert first.result_id == second.result_id
    async with transaction(session_factory) as session:
        history = (
            await session.execute(
                select(TierHistory).where(TierHistory.player_id == seeded["player"].id)
            )
        ).scalars().all()
    assert len(history) == 1


async def test_discord_pending_result_gets_committed_in_place(session_factory, clean_db):
    seeded = await _seed(session_factory)
    from db.repositories.results import ResultRepository

    async with transaction(session_factory) as session:
        await ResultRepository().insert(
            session,
            result_key="promo-3",
            kind="ticket",
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            promotion_status="discord_pending",
            new_tier_id=seeded["tier"].id,
        )
    await PromotionCommitService().commit_after_discord_success(
        session_factory,
        result_key="promo-3",
        kind="ticket",
        player_id=seeded["player"].id,
        kit_id=seeded["kit"].id,
        new_tier_id=seeded["tier"].id,
        discord_role_id=777779,
    )
    async with transaction(session_factory) as session:
        result = await ResultRepository().get_by_key(session, "promo-3")
    assert result.promotion_status == "committed"


async def test_commit_closes_ticket(session_factory, clean_db):
    seeded = await _seed(session_factory)
    from db.repositories.tickets import TicketRepository

    async with transaction(session_factory) as session:
        await TicketRepository().open(
            session, channel_id=555123, player_id=seeded["player"].id,
            ign="Promo", kit_id=seeded["kit"].id
        )
    await PromotionCommitService().commit_after_discord_success(
        session_factory,
        result_key="promo-4",
        kind="ticket",
        player_id=seeded["player"].id,
        kit_id=seeded["kit"].id,
        new_tier_id=seeded["tier"].id,
        discord_role_id=777780,
        close_ticket_channel_id=555123,
    )
    async with transaction(session_factory) as session:
        ticket = (
            await session.execute(select(Ticket).where(Ticket.channel_id == 555123))
        ).scalar_one()
    assert ticket.status == "closed"


async def test_commit_transaction_rolls_back_completely(session_factory, clean_db):
    seeded = await _seed(session_factory)
    with pytest.raises(IntegrityError):
        await PromotionCommitService().commit_after_discord_success(
            session_factory,
            result_key="promo-fail",
            kind="ticket",
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            new_tier_id=seeded["tier"].id,
            discord_role_id=777781,
            cooldowns=(
                CooldownSpec(cooldown_type="bogus_type", expires_at=datetime.now(timezone.utc)),
            ),
        )
    async with transaction(session_factory) as session:
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "promo-fail")
            )
        ).scalar_one_or_none()
        mirror = await MirrorRepository().list_current(session)
        audit_logs = (await session.execute(select(AuditLog))).scalars().all()
    assert result is None
    assert mirror == []
    assert audit_logs == []


async def test_wedge_survives_failed_main_transaction(session_factory, clean_db):
    seeded = await _seed(session_factory)
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-wedge",
        payload={"new_tier_id": seeded["tier"].id,
                 "player_id": seeded["player"].id,
                 "kit_id": seeded["kit"].id},
        discord_role_confirmed=True,
    )
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status="pending")
    assert len(events) == 1
    assert events[0].aggregate_id == "promo-wedge"
    assert events[0].discord_role_confirmed is True


async def test_wedge_written_on_separate_session_after_failure(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    with pytest.raises(IntegrityError):
        await PromotionCommitService().commit_after_discord_success(
            session_factory,
            result_key="promo-wedge-2",
            kind="ticket",
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            new_tier_id=seeded["tier"].id,
            discord_role_id=777782,
            cooldowns=(
                CooldownSpec(cooldown_type="bogus_type", expires_at=datetime.now(timezone.utc)),
            ),
        )
    await enqueue_promotion_wedge(
        session_factory,
        result_key="promo-wedge-2",
        payload={"new_tier_id": seeded["tier"].id},
    )
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status="pending")
        result = (
            await session.execute(
                select(Result).where(Result.result_key == "promo-wedge-2")
            )
        ).scalar_one_or_none()
    assert [e.aggregate_id for e in events] == ["promo-wedge-2"]
    assert result is None

async def test_wedge_helper_commits_after_discord_success(session_factory, clean_db):
    """Cog-side helper: najde hráče, vyřeší dimenze a zapíše mirror."""
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        _, evaluator = await PlayerRepository().claim_discord_id(
            session, discord_id=2222, ign="TesterPromo"
        )
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-1",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777790,
        score="3-1",
        outcome="Won",
        evaluator_discord_id=2222,
        notes="ok",
        date="2026-09-25",
        audit_actor_id=42,
        audit_actor_name="Mod",
    )
    assert isinstance(outcome, PromotionWedgeOutcome)
    assert outcome.committed is True
    assert outcome.wedged is False
    async with transaction(session_factory) as session:
        result = (await session.execute(
            select(Result).where(Result.result_key == "cog-1")
        )).scalar_one()
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        events = await OutboxRepository().list_by_status(session, status="pending")
    assert result.new_tier_id == seeded["tier"].id
    assert result.evaluator_id == evaluator.id
    assert result.score == "3-1"
    assert mirror is not None and mirror.source == "promotion"
    assert events == []


async def test_wedge_helper_claims_new_player(session_factory, clean_db):
    """Discord ID bez záznamu → vytvoří se hráč (stabilní identita)."""
    await _seed(session_factory)
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-2",
        kind="queue",
        discord_id=3333,
        ign="Novacek",
        kit_key="ht3",
        new_tier_code="t1",
        discord_role_id=777791,
    )
    assert outcome.committed is True
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, 3333)
        result = (await session.execute(
            select(Result).where(Result.result_key == "cog-2")
        )).scalar_one()
    assert player is not None and player.ign == "Novacek"
    assert result.player_id == player.id


async def test_wedge_helper_resolves_bridge_tier_code(session_factory, clean_db):
    """bridge_tier_code (kód, např. "LT2") se vyřeší na tier definition ID."""
    await _seed(session_factory)
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-bridge-1",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777792,
        bridge_tier_code="t1",
    )
    assert outcome.committed is True
    async with transaction(session_factory) as session:
        result = (await session.execute(
            select(Result).where(Result.result_key == "cog-bridge-1")
        )).scalar_one()
        bridge = (await session.execute(
            select(TierDefinition).where(TierDefinition.code == "t1")
        )).scalar_one()
    assert result.bridge_tier_id == bridge.id


async def test_wedge_helper_unknown_bridge_code_is_none(session_factory, clean_db):
    """Neznámý bridge kód = volitelná dimenze, NEcommit se neruší."""
    await _seed(session_factory)
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-bridge-2",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777793,
        bridge_tier_code="neexistuje",
    )
    assert outcome.committed is True
    async with transaction(session_factory) as session:
        result = (await session.execute(
            select(Result).where(Result.result_key == "cog-bridge-2")
        )).scalar_one()
    assert result.bridge_tier_id is None


async def test_wedge_helper_missing_dimension_is_loud(session_factory, clean_db):
    """Chybějící kit/tier → NEcommit, ALE wedge (C2 audit fix): Discord se
    už změnil, takže i tenhle případ musí být durably recoverable, jakmile
    admin chybějící kit/tier doplní — ne jen zaloguje a zapomene."""
    await _seed(session_factory)
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-3",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="neexistuje",
        new_tier_code="t2",
        discord_role_id=777792,
    )
    assert outcome.committed is False
    assert outcome.wedged is True
    assert "chybí dimenze" in outcome.message
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status="pending")
        mirror = await MirrorRepository().list_current(session)
    assert len(events) == 1
    assert events[0].payload["resolved"] is False
    assert events[0].payload["kit_key"] == "neexistuje"
    assert mirror == []


async def test_wedge_helper_no_session_factory_is_loud():
    """Bez nakonfigurovaného PostgreSQL: hlasitě, bez mirroru i bez outboxu."""
    outcome = await commit_promotion_with_wedge(
        None,
        result_key="cog-4",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777793,
    )
    assert outcome.committed is False
    assert outcome.wedged is False
    assert "PostgreSQL" in outcome.message


async def test_wedge_helper_identity_conflict_is_loud(session_factory, clean_db):
    """IGN patří jinému hráči → odmítnout, mirror se nepíše, ALE wedge (C2
    audit fix): Discord se už změnil, takže i konflikt identity musí nechat
    durable stopu pro ruční review, ne jen log řádek."""
    await _seed(session_factory)
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-5",
        kind="ticket",
        discord_id=4444,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777794,
    )
    assert outcome.committed is False
    assert outcome.wedged is True
    assert "identita" in outcome.message
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status="pending")
    assert len(events) == 1
    assert events[0].payload["resolved"] is False


async def test_wedge_helper_db_failure_wedges_and_consumer_recovers(
    session_factory, clean_db, monkeypatch
):
    """Selže-li commit, wedge jde do outboxu a consumer mirror doplní."""
    seeded = await _seed(session_factory)
    real = PromotionCommitService.commit_after_discord_success

    async def boom(*args, **kwargs):
        raise RuntimeError("DB outage")

    monkeypatch.setattr(PromotionCommitService, "commit_after_discord_success", boom)
    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="cog-6",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777795,
        score="3-1",
        outcome="Won",
        audit_actor_id=42,
        audit_actor_name="Mod",
    )
    monkeypatch.setattr(
        PromotionCommitService, "commit_after_discord_success", real
    )
    assert outcome.committed is False
    assert outcome.wedged is True
    assert "outboxu" in outcome.message
    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status="pending")
    assert len(events) == 1
    assert events[0].event_type == "promotion_commit"
    assert events[0].discord_role_confirmed is True
    payload = events[0].payload
    assert payload["version"] == 1
    assert payload["result_key"] == "cog-6"
    assert payload["new_tier_id"] == seeded["tier"].id
    assert payload["score"] == "3-1"

    consumption = await OutboxConsumer().consume_one(session_factory)
    assert consumption is not None and consumption.outcome == CONSUMED_DONE
    async with transaction(session_factory) as session:
        result = (await session.execute(
            select(Result).where(Result.result_key == "cog-6")
        )).scalar_one()
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        events = await OutboxRepository().list_by_status(session, status="done")
    assert result.promotion_status == "committed"
    assert mirror is not None and mirror.source == "promotion"
    assert len(events) == 1 and events[0].status == OUTBOX_DONE

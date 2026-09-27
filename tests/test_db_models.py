"""ORM model roundtrips, constraints and transaction boundaries."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from db.engine import make_session_factory
from db.models import (
    Cooldown,
    Kit,
    OutboxEvent,
    Player,
    PlayerCurrentTier,
    Result,
    SyncRun,
    TierDefinition,
    TierHistory,
)


async def _seed(engine, session_factory, player=True, kit=True, tier=True):
    async with session_factory() as session:
        session.add_all(
            [
                item
                for item in [
                    Player(discord_id=111, ign="PlayerOne", source="migration")
                    if player
                    else None,
                    Kit(name="MolePVP", key="molepvp") if kit else None,
                    TierDefinition(
                        code="HT3", kind="ladder", rank=1, display_name="HT3"
                    )
                    if tier
                    else None,
                ]
                if item is not None
            ]
        )
        await session.commit()
    async with engine.connect() as conn:
        return (
            (await conn.exec_driver_sql("SELECT id FROM players LIMIT 1")).scalar(),
            (await conn.exec_driver_sql("SELECT id FROM kits LIMIT 1")).scalar(),
            (await conn.exec_driver_sql("SELECT id FROM tier_definitions LIMIT 1")).scalar(),
        )


async def test_player_roundtrip_with_utc_timestamps(db_engine, clean_db):
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add(
            Player(discord_id=222, ign="Roundtrip", source="discord")
        )
        await session.commit()
        fetched = (
            await session.execute(
                Player.__table__.select().where(Player.discord_id == 222)
            )
        ).first()
    assert fetched.ign == "Roundtrip"
    assert fetched.created_at.tzinfo is not None
    assert fetched.created_at.utcoffset() == timezone.utc.utcoffset(None)


async def test_current_tier_unique_per_player_kit(db_engine, clean_db):
    player_id, kit_id, tier_id = await _seed(db_engine, make_session_factory(db_engine))
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add_all(
            [
                PlayerCurrentTier(
                    player_id=player_id,
                    kit_id=kit_id,
                    tier_id=tier_id,
                    discord_role_id=999,
                    observed_at=datetime.now(timezone.utc),
                    source="manual",
                ),
                PlayerCurrentTier(
                    player_id=player_id,
                    kit_id=kit_id,
                    tier_id=tier_id,
                    discord_role_id=999,
                    observed_at=datetime.now(timezone.utc),
                    source="manual",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_partial_unique_waitlist_cooldown(db_engine, clean_db):
    player_id, _, _ = await _seed(db_engine, make_session_factory(db_engine))
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add_all(
            [
                Cooldown(
                    player_id=player_id,
                    cooldown_type="waitlist",
                    expires_at=datetime.now(timezone.utc),
                ),
                Cooldown(
                    player_id=player_id,
                    cooldown_type="waitlist",
                    expires_at=datetime.now(timezone.utc),
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_check_constraint_rejects_bad_status(db_engine, clean_db):
    player_id, kit_id, tier_id = await _seed(db_engine, make_session_factory(db_engine))
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add(
            Result(
                result_key="x:1",
                kind="bogus-kind",
                player_id=player_id,
                kit_id=kit_id,
                recorded_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_check_constraint_rejects_bad_outbox_status(db_engine, clean_db):
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add(
            OutboxEvent(
                event_type="promotion_commit",
                aggregate_type="result",
                aggregate_id="k",
                payload={"ok": True},
                status="nonsense",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_foreign_key_violation_rejected(db_engine, clean_db):
    _, kit_id, tier_id = await _seed(
        db_engine, make_session_factory(db_engine), player=False
    )
    factory = make_session_factory(db_engine)
    async with factory() as session:
        session.add(
            PlayerCurrentTier(
                player_id=999999,
                kit_id=kit_id,
                tier_id=tier_id,
                observed_at=datetime.now(timezone.utc),
                source="manual",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_promotion_pipeline_roundtrip(db_engine, clean_db):
    player_id, kit_id, tier_id = await _seed(db_engine, make_session_factory(db_engine))
    factory = make_session_factory(db_engine)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        result = Result(
            result_key="ticket:1",
            kind="ticket",
            player_id=player_id,
            kit_id=kit_id,
            previous_tier_id=None,
            new_tier_id=tier_id,
            promotion_status="committed",
            recorded_at=now,
        )
        current = PlayerCurrentTier(
            player_id=player_id,
            kit_id=kit_id,
            tier_id=tier_id,
            discord_role_id=999,
            observed_at=now,
            source="promotion",
            result_id=None,
        )
        history = TierHistory(
            player_id=player_id,
            kit_id=kit_id,
            tier_id=tier_id,
            previous_tier_id=None,
            changed_at=now,
            source="promotion",
        )
        session.add_all([result, current, history])
        await session.commit()
        result_id = result.id

    async with factory() as session:
        fetched = (
            await session.execute(
                Result.__table__.select().where(Result.id == result_id)
            )
        ).first()
    assert fetched.promotion_status == "committed"
    assert fetched.result_key == "ticket:1"


async def test_transaction_rollback_discards_changes(db_engine, clean_db):
    player_id, kit_id, tier_id = await _seed(db_engine, make_session_factory(db_engine))
    factory = make_session_factory(db_engine)
    with pytest.raises(RuntimeError):
        async with factory() as session:
            session.add(
                SyncRun(command="sync_discord", mode="apply")
            )
            await session.flush()
            raise RuntimeError("boom")
    async with factory() as session:
        count = (
            await session.execute(SyncRun.__table__.select())
        ).all()
    assert count == []
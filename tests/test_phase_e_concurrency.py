"""Phase E, E11 — concurrency: unique constraints, transaction boundaries, outbox locking.

Real concurrency on embedded PostgreSQL. Every scenario opens independent
sessions (``NullPool`` gives each session its own connection) so the DB — not
in-process serialization — arbitrates the races:

1. Duplicate IGN (``lower(ign)`` unique): two concurrent creates → exactly one
   row survives, the loser raises ``IntegrityError``.
2. Duplicate Discord ID (partial unique index): two concurrent claims of the
   same ID → exactly one player row.
3. Mirror ``(player_id, kit_id)`` unique: two concurrent observations of the
   same key → exactly one ``player_current_tiers`` row and exactly one
   ``tier_history`` row (the loser is rolled back wholesale, no orphan history).
4. Cooldown partial unique indexes: two concurrent waitlist inserts for the
   same player → one row; a duplicate kit cooldown is rejected; waitlist and
   kit cooldowns coexist (different partial scopes).
5. Transaction boundary: an exception inside ``transaction()`` rolls back the
   whole unit — the mirror + history written before the failure disappear
   together (no partial write, invariant of the phase).
6. Outbox ``FOR UPDATE SKIP LOCKED``: N concurrent consumers claim N pending
   events — every event is claimed exactly once and no consumer gets ``None``
   while events remain unclaimed; an extra consumer gets ``None``.

``clean_db`` is required so each test sees an empty schema; ``session_factory``
comes from conftest (alembic-migrated ``pytest_phase_a``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.models import Cooldown, Kit, Player, PlayerCurrentTier, TierDefinition, TierHistory
from db.models import OutboxEvent
from db.repositories.outbox import OutboxRepository
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction


async def _seed_dimensions(session_factory):
    from db.repositories.kits import ensure_dimensions

    async with session_factory() as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t1 = (
            await session.execute(select(TierDefinition).where(TierDefinition.code == "t1"))
        ).scalar_one()
        t2 = (
            await session.execute(select(TierDefinition).where(TierDefinition.code == "t2"))
        ).scalar_one()
        player = (
            await session.execute(select(Player).where(Player.discord_id == 1111))
        ).scalar_one_or_none()
        if player is None:
            player = Player(discord_id=1111, ign="Concurrent", source="discord")
            session.add(player)
        await session.commit()
        return {"kit": kit, "t1": t1, "t2": t2, "player": player}


async def _count_groups(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        return {
            "players": (
                await session.execute(select(func.count()).select_from(Player))
            ).scalar_one(),
            "current_tiers": (
                await session.execute(
                    select(func.count()).select_from(PlayerCurrentTier)
                )
            ).scalar_one(),
            "history": (
                await session.execute(
                    select(func.count()).select_from(TierHistory)
                )
            ).scalar_one(),
            "cooldowns": (
                await session.execute(
                    select(func.count()).select_from(Cooldown)
                )
            ).scalar_one(),
        }


async def test_concurrent_duplicate_ign_keeps_single_row(session_factory, clean_db):
    async def create(name: str) -> None:
        async with transaction(session_factory) as session:
            await PlayerRepository().get_or_create_by_ign(
                session, ign=name, source="migration"
            )

    outcomes = await asyncio.gather(
        create("AliceMC"), create("alicemc"), return_exceptions=True
    )
    assert sum(1 for o in outcomes if isinstance(o, IntegrityError)) == 1
    assert sum(1 for o in outcomes if o is None) == 1

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Player).where(func.lower(Player.ign) == "alicemc")
            )
        ).scalars().all()
    assert len(rows) == 1, "lower(ign) unique — duplicitní IGN musí zanechat jeden řádek"


async def test_concurrent_same_discord_id_keeps_single_row(session_factory, clean_db):
    """M2 audit fix: claim_discord_id now retries a lost unique-constraint
    race (via a per-attempt SAVEPOINT) instead of surfacing a raw
    IntegrityError — both concurrent claims of the same brand-new
    discord_id succeed (one CREATED, one sees the winner and returns
    UNCHANGED/RENAMED), never a bare DB exception."""
    async def claim(ign: str) -> str:
        async with session_factory() as session:
            outcome, _player = await PlayerRepository().claim_discord_id(
                session, discord_id=900001, ign=ign
            )
            await session.commit()
            return outcome

    outcomes = await asyncio.gather(
        claim("First"), claim("Second"), return_exceptions=True
    )
    assert not any(isinstance(o, Exception) for o in outcomes), (
        f"identity race must resolve without a raw exception: {outcomes}"
    )

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Player).where(Player.discord_id == 900001)
            )
        ).scalars().all()
    assert len(rows) == 1, "uq_players_discord_id — stejné Discord ID nesmí vzniknout dvakrát"


async def test_concurrent_mirror_apply_serializes_via_advisory_lock(
    session_factory, clean_db
):
    """H4 audit fix: apply_observation takes a transaction-scoped Postgres
    advisory lock keyed by (player_id, kit_id), so two concurrent
    observations/promotions for the same player+kit SERIALIZE instead of
    racing — the second call always sees the first call's committed mirror
    row before deciding tier_changed / previous_tier_id, so tier_history can
    never end up with two rows claiming the same stale previous_tier_id.

    Before the fix, both calls would read the same pre-image concurrently,
    both decide "first observation", and race to INSERT — the DB unique
    constraint rejected the loser (IntegrityError), but a slightly different
    interleaving (e.g. after the fix is later weakened, or on a mirror
    update instead of insert) could let two transitions commit with
    identical, both-stale previous_tier_id values — a silent corruption of
    the documented append-only, authoritative history table. This test
    proves the lock, not the unique constraint, is what prevents that now:
    with two DIFFERENT target tiers requested concurrently for a brand-new
    (player, kit), both calls succeed (no IntegrityError), and history ends
    up with exactly two consistent, correctly-chained rows.
    """
    seeded = await _seed_dimensions(session_factory)
    now = datetime.now(timezone.utc)

    async def observe(tier_id: int) -> object:
        async with transaction(session_factory) as session:
            return await MirrorServiceRepository().apply_observation(
                session,
                player_id=seeded["player"].id,
                kit_id=seeded["kit"].id,
                tier_id=tier_id,
                observed_at=now,
                source="discord_sync",
            )

    outcomes = await asyncio.gather(
        observe(seeded["t1"].id), observe(seeded["t2"].id), return_exceptions=True
    )
    assert not any(isinstance(o, Exception) for o in outcomes), (
        "advisory lock musí souběžné observace serializovat, ne nechat "
        f"jednu spadnout na IntegrityError: {outcomes}"
    )

    counts = await _count_groups(session_factory)
    assert counts["current_tiers"] == 1, "přesně jedna aktualní tier řádka"
    assert counts["history"] == 2, (
        "obě observace jsou legitimní přechody (různé cílové tiery) — "
        "očekávají se dvě navazující history řádky, ne osiřelá/duplicitní"
    )

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(TierHistory)
                .where(
                    TierHistory.player_id == seeded["player"].id,
                    TierHistory.kit_id == seeded["kit"].id,
                )
                .order_by(TierHistory.id)
            )
        ).scalars().all()
    origin = next(r for r in rows if r.previous_tier_id is None)
    transition = next(r for r in rows if r.previous_tier_id is not None)
    assert transition.previous_tier_id == origin.tier_id, (
        "druhá (později commitnutá) observace musí navazovat na skutečně "
        "commitnutý stav první observace, ne na shodný stale pre-image"
    )

    async with session_factory() as session:
        mirror = (
            await session.execute(
                select(PlayerCurrentTier).where(
                    PlayerCurrentTier.player_id == seeded["player"].id,
                    PlayerCurrentTier.kit_id == seeded["kit"].id,
                )
            )
        ).scalar_one()
    assert mirror.tier_id == transition.tier_id, (
        "finální mirror musí odpovídat poslední (transition) history řádce"
    )


async def test_concurrent_mirror_apply_same_tier_no_duplicate_transition(
    session_factory, clean_db
):
    """H4: two concurrent observations of the SAME target tier for a brand
    new (player, kit) must serialize into exactly one 'first observation'
    history row — the second call must see the first's committed row and
    correctly no-op (tier_changed=False), never create a duplicate/
    conflicting transition row."""
    seeded = await _seed_dimensions(session_factory)
    now = datetime.now(timezone.utc)

    async def observe() -> object:
        async with transaction(session_factory) as session:
            return await MirrorServiceRepository().apply_observation(
                session,
                player_id=seeded["player"].id,
                kit_id=seeded["kit"].id,
                tier_id=seeded["t1"].id,
                observed_at=now,
                source="discord_sync",
            )

    outcomes = await asyncio.gather(observe(), observe(), return_exceptions=True)
    assert not any(isinstance(o, Exception) for o in outcomes), outcomes

    counts = await _count_groups(session_factory)
    assert counts["current_tiers"] == 1
    assert counts["history"] == 1, (
        "stejný cílový tier dvakrát souběžně nesmí vytvořit duplicitní "
        "history řádku — druhá observace je no-op (tier_changed=False)"
    )


async def test_concurrent_waitlist_cooldown_single_row(session_factory, clean_db):
    seeded = await _seed_dimensions(session_factory)
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)

    async def insert_waitlist() -> None:
        async with transaction(session_factory) as session:
            session.add(
                Cooldown(
                    player_id=seeded["player"].id,
                    cooldown_type="waitlist",
                    kit_id=None,
                    expires_at=future,
                    source="auto",
                )
            )

    outcomes = await asyncio.gather(
        insert_waitlist(), insert_waitlist(), return_exceptions=True
    )
    assert sum(1 for o in outcomes if isinstance(o, IntegrityError)) == 1
    assert sum(1 for o in outcomes if o is None) == 1

    counts = await _count_groups(session_factory)
    assert counts["cooldowns"] == 1, "uq_cooldowns_waitlist — duplicitní waitlist odmítnut"


async def test_cooldown_partial_unique_scopes(session_factory, clean_db):
    """waitlist (kit_id NULL) a ht3 (kit_id NOT NULL) mají oddělené indexy."""
    seeded = await _seed_dimensions(session_factory)
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)

    async with transaction(session_factory) as session:
        session.add(
            Cooldown(
                player_id=seeded["player"].id,
                cooldown_type="waitlist",
                kit_id=None,
                expires_at=future,
                source="auto",
            )
        )
        session.add(
            Cooldown(
                player_id=seeded["player"].id,
                cooldown_type="ht3",
                kit_id=seeded["kit"].id,
                expires_at=future,
                source="auto",
            )
        )
    counts = await _count_groups(session_factory)
    assert counts["cooldowns"] == 2, "waitlist a ht3 koexistují (scoped partial unique)"

    # Duplicitní kit cooldown pro stejné (player, kit, type) je odmítnut.
    with pytest.raises(IntegrityError):
        async with transaction(session_factory) as session:
            session.add(
                Cooldown(
                    player_id=seeded["player"].id,
                    cooldown_type="ht3",
                    kit_id=seeded["kit"].id,
                    expires_at=future,
                    source="auto",
                )
            )
    counts = await _count_groups(session_factory)
    assert counts["cooldowns"] == 2, "uq_cooldowns_kit — duplicitní kit cooldown odmítnut"


async def test_transaction_boundary_rolls_back_everything(session_factory, clean_db):
    """Selhání uvnitř transaction() zruší mirror i history navzdory flush."""
    seeded = await _seed_dimensions(session_factory)
    now = datetime.now(timezone.utc)

    with pytest.raises(RuntimeError):
        async with transaction(session_factory) as session:
            await MirrorServiceRepository().apply_observation(
                session,
                player_id=seeded["player"].id,
                kit_id=seeded["kit"].id,
                tier_id=seeded["t1"].id,
                observed_at=now,
                source="discord_sync",
            )
            raise RuntimeError("simulovaný pád uprostřed transakce")

    counts = await _count_groups(session_factory)
    assert counts["current_tiers"] == 0, "rollback — mirror se nezapíše"
    assert counts["history"] == 0, "rollback — history se nezapíše"
    assert counts["players"] == 1, "seed zůstává (commit v _seed_dimensions)"


async def test_concurrent_ticket_open_same_player_kit_one_wins_cleanly(
    session_factory, clean_db
):
    """M5 audit fix: two concurrent create_ticket() calls for the same
    player+kit must race safely — the DB unique index rejects one, but the
    command layer (services.tickets._db_create_ticket) must convert that
    into a clean 'duplicate' result, never a raw, unhandled IntegrityError
    reaching the Discord command handler."""
    from services.tickets import create_ticket

    await _seed_dimensions(session_factory)

    async def open_ticket(channel_id: int) -> dict:
        return await create_ticket(
            channel_id=channel_id,
            owner_id="1111",
            owner_name="Concurrent",
            ign="Concurrent",
            kit="ht3",
            # target_tier=None sidesteps _db_resolve_tier's own separate
            # get-or-create race (a different, pre-existing race in tier
            # auto-registration, not what this test targets) — this test
            # is specifically about the ticket-open unique-index race.
            target_tier=None,
            current_tier=None,
            eval_ok=False,
            category_id=1,
            now=1_700_000_000_000,
            session_factory=session_factory,
        )

    outcomes = await asyncio.gather(
        open_ticket(9001), open_ticket(9002), return_exceptions=True
    )
    assert not any(isinstance(o, Exception) for o in outcomes), (
        f"a lost ticket-open race must never surface as a raw exception: {outcomes}"
    )
    results = {o["result"] for o in outcomes}
    assert results == {"created", "duplicate"}, outcomes


async def test_outbox_claim_each_event_exactly_once(session_factory, clean_db):
    """FOR UPDATE SKIP LOCKED: N konzumentů si nerozběhne stejný event."""
    seeded = await _seed_dimensions(session_factory)
    async with transaction(session_factory) as session:
        for i in range(5):
            await OutboxRepository().enqueue(
                session,
                event_type="tier_change",
                aggregate_type="player",
                aggregate_id=str(seeded["player"].id),
                payload={"n": i, "tier": "t1"},
            )

    async def consumer() -> list[int]:
        claimed: list[int] = []
        async with session_factory() as session:
            while True:
                row = await OutboxRepository().claim_next(session)
                if row is None:
                    break
                claimed.append(row.id)
                await session.commit()
        return claimed

    results = await asyncio.gather(*[consumer() for _ in range(5)])
    all_ids = [i for r in results for i in r]
    assert len(all_ids) == 5, "každý event je claimnut právě jednou"
    assert len(set(all_ids)) == 5, "žádný event nesmí být claimnut dvěma konzumenty"

    async with session_factory() as session:
        statuses = (
            await session.execute(
                select(OutboxEvent.status).order_by(OutboxEvent.id)
            )
        ).scalars().all()
    assert statuses == ["in_progress"] * 5


async def test_outbox_extra_consumer_gets_none(session_factory, clean_db):
    """Šestý konzument na 5 eventů najde po vyčerpání prázdno."""
    seeded = await _seed_dimensions(session_factory)
    async with transaction(session_factory) as session:
        for i in range(5):
            await OutboxRepository().enqueue(
                session,
                event_type="tier_change",
                aggregate_type="player",
                aggregate_id=str(seeded["player"].id),
                payload={"n": i},
            )

    async def consumer() -> list[int]:
        claimed: list[int] = []
        async with session_factory() as session:
            while True:
                row = await OutboxRepository().claim_next(session)
                if row is None:
                    break
                claimed.append(row.id)
                await session.commit()
        return claimed

    results = await asyncio.gather(*[consumer() for _ in range(6)])
    lengths = sorted(len(r) for r in results)
    assert lengths == [0, 1, 1, 1, 1, 1], (
        "přesně pět eventů rozdělených mezi konzumenty, jeden najde prázdno"
    )
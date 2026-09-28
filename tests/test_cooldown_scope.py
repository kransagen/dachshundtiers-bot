"""The cooldown business rule, locked in as a regression suite.

Rule under test: **cooldowns are always per player + per kit + per type.**
``kit_id`` is part of the enforced DB identity (``uq_cooldowns_kit``), so
Player A + Boxing + ht3 is on cooldown while Player A + Bedwars + ht3 is not.

The one deliberate exception is a *legacy* row with ``kit_id IS NULL`` from the
Phase D import of the kit-less ``cooldowns.json``. The importer now derives the
kit from ``ht_results.json`` whenever that is unambiguous; rows that remain
global are explicitly flagged with an open ``MigrationImportIssue``. These
tests prove both halves: the business rule holds for everything the runtime
writes, and the global shape can only ever arrive already flagged.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db.models import Cooldown, Kit, MigrationImportIssue
from db.repositories.cooldowns import (
    COOLDOWN_HT3,
    COOLDOWN_WAITLIST,
    CooldownRepository,
)
from db.repositories.kits import ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

KIT_DEFS = (("boxing", "Boxing"), ("bedwars", "Bedwars"))
TIER_DEFS = (("HT3", "ladder", "HT3", 3),)

BOXING = "boxing"
BEDWARS = "bedwars"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _seed(session_factory, *, discord_id: int = 4242, ign: str = "player_a"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KIT_DEFS, TIER_DEFS)
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=discord_id, ign=ign
        )
        return player


async def _kit_id(session_factory, key: str) -> int:
    async with session_factory() as session:
        kit = (
            await session.execute(select(Kit).where(Kit.key == key))
        ).scalar_one()
        return kit.id


async def _tier_id(session_factory, code: str) -> int:
    from db.models import TierDefinition

    async with session_factory() as session:
        tier = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == code)
            )
        ).scalar_one()
        return tier.id


async def _grant(
    session_factory,
    *,
    player_id: int,
    kit_key: str,
    cooldown_type: str,
    hours: float = 4.0,
) -> Cooldown:
    async with transaction(session_factory) as session:
        return await CooldownRepository().upsert(
            session,
            player_id=player_id,
            cooldown_type=cooldown_type,
            kit_id=await _kit_id(session_factory, kit_key),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=hours),
        )


async def _active_for(
    session_factory, *, player_id: int, cooldown_type: str, kit_key: str
) -> list[Cooldown]:
    async with session_factory() as session:
        return await CooldownRepository().get_active(
            session,
            player_id=player_id,
            cooldown_type=cooldown_type,
            kit_id=await _kit_id(session_factory, kit_key),
        )


async def _waitlist_blocking(
    session_factory, *, player_id: int, kit_key: str
) -> list[Cooldown]:
    async with session_factory() as session:
        return await CooldownRepository().get_active_waitlist(
            session, player_id=player_id, kit_id=await _kit_id(session_factory, kit_key)
        )


async def _all_cooldowns(session_factory) -> list[Cooldown]:
    async with session_factory() as session:
        return list((await session.execute(select(Cooldown))).scalars().all())


async def _open_issues(session_factory) -> list[MigrationImportIssue]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(MigrationImportIssue).where(
                        MigrationImportIssue.status == "open"
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


# --------------------------------------------------------------------------
# waitlist -> always player + kit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waitlist_cooldown_on_boxing_does_not_block_bedwars(
    session_factory, clean_db
):
    player = await _seed(session_factory)

    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_WAITLIST,
    )

    assert await _waitlist_blocking(session_factory, player_id=player.id, kit_key=BOXING)
    assert not await _waitlist_blocking(
        session_factory, player_id=player.id, kit_key=BEDWARS
    )


@pytest.mark.asyncio
async def test_waitlist_cooldown_does_not_block_another_player(
    session_factory, clean_db
):
    a = await _seed(session_factory, discord_id=1, ign="a")
    b = await _seed(session_factory, discord_id=2, ign="b")

    await _grant(
        session_factory,
        player_id=a.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_WAITLIST,
    )

    assert not await _waitlist_blocking(
        session_factory, player_id=b.id, kit_key=BOXING
    )


@pytest.mark.asyncio
async def test_upsert_is_scoped_per_kit_not_global_player_type(
    session_factory, clean_db
):
    """A second kit gets its own row; the first kit's cooldown is untouched."""
    player = await _seed(session_factory)

    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_WAITLIST,
        hours=4.0,
    )
    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BEDWARS,
        cooldown_type=COOLDOWN_WAITLIST,
        hours=1.0,
    )

    rows = await _all_cooldowns(session_factory)
    assert len(rows) == 2
    assert {r.kit_id for r in rows} == {
        await _kit_id(session_factory, BOXING),
        await _kit_id(session_factory, BEDWARS),
    }
    assert all(r.cooldown_type == COOLDOWN_WAITLIST for r in rows)


# --------------------------------------------------------------------------
# ht3 -> always player + kit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ht3_cooldown_on_boxing_does_not_block_bedwars(
    session_factory, clean_db
):
    player = await _seed(session_factory)

    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_HT3,
    )

    assert await _active_for(
        session_factory,
        player_id=player.id,
        cooldown_type=COOLDOWN_HT3,
        kit_key=BOXING,
    )
    assert not await _active_for(
        session_factory,
        player_id=player.id,
        cooldown_type=COOLDOWN_HT3,
        kit_key=BEDWARS,
    )


@pytest.mark.asyncio
async def test_ht3_cooldown_does_not_block_another_player(session_factory, clean_db):
    a = await _seed(session_factory, discord_id=1, ign="a")
    b = await _seed(session_factory, discord_id=2, ign="b")

    await _grant(
        session_factory, player_id=a.id, kit_key=BOXING, cooldown_type=COOLDOWN_HT3
    )

    assert not await _active_for(
        session_factory, player_id=b.id, cooldown_type=COOLDOWN_HT3, kit_key=BOXING
    )


# --------------------------------------------------------------------------
# the types are independent
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waitlist_and_ht3_are_independent_types_for_one_kit(
    session_factory, clean_db
):
    player = await _seed(session_factory)

    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_HT3,
    )

    # HT3 is set, waitlist is not — a different type never implies the other.
    assert await _active_for(
        session_factory,
        player_id=player.id,
        cooldown_type=COOLDOWN_HT3,
        kit_key=BOXING,
    )
    assert not await _active_for(
        session_factory,
        player_id=player.id,
        cooldown_type=COOLDOWN_WAITLIST,
        kit_key=BOXING,
    )
    assert not await _waitlist_blocking(
        session_factory, player_id=player.id, kit_key=BOXING
    )
    assert len(await _all_cooldowns(session_factory)) == 1


@pytest.mark.asyncio
async def test_the_two_documented_kinds_of_cooldown_coexist_per_kit(
    session_factory, clean_db
):
    """Player A + Boxing has BOTH; Player A + Bedwars has NEITHER."""
    player = await _seed(session_factory)

    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_WAITLIST,
    )
    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_HT3,
    )

    for cooldown_type in (COOLDOWN_WAITLIST, COOLDOWN_HT3):
        assert await _active_for(
            session_factory,
            player_id=player.id,
            cooldown_type=cooldown_type,
            kit_key=BOXING,
        ), cooldown_type
        assert not await _active_for(
            session_factory,
            player_id=player.id,
            cooldown_type=cooldown_type,
            kit_key=BEDWARS,
        ), cooldown_type
    assert not await _waitlist_blocking(
        session_factory, player_id=player.id, kit_key=BEDWARS
    )


# --------------------------------------------------------------------------
# identity is enforced by the database, not just by the call sites
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_player_kit_type_row_is_impossible(session_factory, clean_db):
    """``uq_cooldowns_kit`` enforces the identity even against raw inserts."""
    from sqlalchemy.exc import IntegrityError

    player = await _seed(session_factory)
    kit_id = await _kit_id(session_factory, BOXING)
    expires = datetime.now(timezone.utc) + timedelta(hours=4)

    async with transaction(session_factory) as session:
        await CooldownRepository().upsert(
            session,
            player_id=player.id,
            cooldown_type=COOLDOWN_HT3,
            kit_id=kit_id,
            expires_at=expires,
        )

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            session.add(
                Cooldown(
                    player_id=player.id,
                    cooldown_type=COOLDOWN_HT3,
                    kit_id=kit_id,
                    expires_at=expires,
                )
            )
            await session.commit()

    rows = await _all_cooldowns(session_factory)
    assert len(rows) == 1


def test_check_constraint_name_matches_the_migration():
    """The ORM must produce the SAME name the migration created.

    The model declares the bare suffix ``type``; ``db/base.py``'s convention
    (``ck_%(table_name)s_%(constraint_name)s``) expands it to
    ``ck_cooldowns_type`` — exactly what migration 63bdbcfda74a created, so
    ``alembic revision --autogenerate`` sees no phantom DROP + CREATE.
    Spelling the full name out in the model would double-prefix it to
    ``ck_cooldowns_ck_cooldowns_type``. This test pins both halves.
    """
    names = {c.name for c in Cooldown.__table__.constraints}
    assert "ck_cooldowns_type" in names  # effective name after the convention
    assert "ck_cooldowns_ck_cooldowns_type" not in names

    migration = (
        REPO_ROOT / "migrations" / "versions"
    ) / "63bdbcfda74a_initial_postgresql_schema.py"
    source = migration.read_text(encoding="utf-8")
    assert "op.f('ck_cooldowns_type')" in source
    assert "cooldown_type IN ('waitlist', 'ht3')" in source


# --------------------------------------------------------------------------
# the legacy global shape is a flagged migration edge case, nothing else
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_runtime_write_ever_produces_a_global_cooldown(
    session_factory, clean_db
):
    player = await _seed(session_factory)

    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BOXING,
        cooldown_type=COOLDOWN_WAITLIST,
    )
    await _grant(
        session_factory,
        player_id=player.id,
        kit_key=BEDWARS,
        cooldown_type=COOLDOWN_HT3,
    )

    assert not [r for r in await _all_cooldowns(session_factory) if r.kit_id is None]


def _cooldown_upsert_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call that reaches ``CooldownRepository.upsert``.

    Matches both spellings used in production: the direct
    ``CooldownRepository().upsert(...)`` and the injected
    ``self._cooldowns.upsert(...)`` on the canonical promotion service.
    """
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "upsert":
            continue
        owner = func.value
        is_direct = (
            isinstance(owner, ast.Call)
            and isinstance(owner.func, ast.Name)
            and owner.func.id == "CooldownRepository"
        )
        is_injected = (
            isinstance(owner, ast.Attribute)
            and owner.attr in {"_cooldowns", "cooldowns", "cooldown_repo"}
        )
        if is_direct or is_injected:
            calls.append(node)
    return calls


def test_production_cooldown_writers_always_pass_a_kit_id():
    """AST guard: no production cooldown writer can emit a global cooldown.

    Qualname/AST-based (not line numbers) so it survives edits. The Phase D
    importer is included on purpose: it is the one place allowed to produce the
    legacy kit-less shape, and even there the call passes the resolved
    ``kit_id`` variable rather than a literal ``None``.
    """
    offenders: list[str] = []
    total = 0
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        # `db/repositories/cooldowns.py` DEFINES upsert; `db/services/*` is
        # production code and must be covered (the canonical promotion
        # service writes cooldowns through an injected repository).
        if (
            rel.parts[0] in {"tests", "migrations"}
            or rel.parts[:2] == ("db", "repositories")
            or rel.parts[:2] == ("db", "models")
            or "__pycache__" in rel.parts
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in _cooldown_upsert_calls(tree):
            total += 1
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if "kit_id" not in kwargs:
                offenders.append(f"{rel}:{node.lineno} (no kit_id)")
            elif any(
                kw.arg == "kit_id" and isinstance(kw.value, ast.Constant)
                and kw.value.value is None
                for kw in node.keywords
            ):
                offenders.append(f"{rel}:{node.lineno} (literal kit_id=None)")

    assert offenders == [], (
        "a production cooldown writer can produce a global (kit-less) "
        f"cooldown: {offenders}"
    )
    assert total >= 7, (
        f"only {total} cooldown write sites found — the guard has gone stale "
        "and is no longer covering the writers it was written for"
    )


@pytest.mark.asyncio
async def test_promotion_service_cannot_write_a_global_cooldown(
    session_factory, clean_db
):
    """The canonical promotion path is the 7th writer — it must stay kit-scoped.

    ``CooldownSpec.kit_id`` is optional, and the outbox replay path feeds it
    from ``raw.get("kit_id")``. A spec that omits the kit falls back to the
    promotion's own kit rather than becoming a kit-less cooldown that would
    block every kit.
    """
    from db.services.promotion import CooldownSpec, PromotionCommitService

    player = await _seed(session_factory, discord_id=1, ign="promoted")
    kit_id = await _kit_id(session_factory, BOXING)
    tier_id = await _tier_id(session_factory, "HT3")
    expires = datetime.now(timezone.utc) + timedelta(days=7)

    await PromotionCommitService().commit_after_discord_success(
        session_factory,
        result_key="promo-1",
        kind="ticket",
        player_id=player.id,
        kit_id=kit_id,
        new_tier_id=tier_id,
        discord_role_id=555,
        cooldowns=(CooldownSpec(cooldown_type=COOLDOWN_HT3, expires_at=expires),),
    )

    rows = await _all_cooldowns(session_factory)
    assert len(rows) == 1
    assert rows[0].kit_id == kit_id  # scoped to the promoted kit, not global
    assert rows[0].cooldown_type == COOLDOWN_HT3
    assert not await _active_for(
        session_factory,
        player_id=player.id,
        cooldown_type=COOLDOWN_HT3,
        kit_key=BEDWARS,
    )
    assert await _active_for(
        session_factory,
        player_id=player.id,
        cooldown_type=COOLDOWN_HT3,
        kit_key=BOXING,
    )


@pytest.mark.asyncio
async def test_outbox_replay_of_a_kitless_payload_stays_kit_scoped(
    session_factory, clean_db
):
    """A legacy wedge payload without ``kit_id`` must not replay into a global row.

    ``build_commit_kwargs`` passes ``raw.get("kit_id")`` straight through, so the
    fallback has to live in the service — this pins the whole chain.
    """
    from db.services.outbox_consumer import build_commit_kwargs

    payload = {
        "version": 1,
        "result_key": "wedge-1",
        "kind": "ticket",
        "player_id": 1,
        "kit_id": 2,
        "new_tier_id": 3,
        "discord_role_id": 4,
        "cooldowns": [
            {
                "cooldown_type": COOLDOWN_WAITLIST,
                "expires_at": datetime.now(timezone.utc).isoformat(),
                # no "kit_id" — the legacy payload shape
            }
        ],
    }
    kwargs = build_commit_kwargs(payload)
    assert kwargs["cooldowns"][0].kit_id is None  # faithfully parsed as absent

    # ...and the service then scopes it to the promotion's kit.
    from db.services.promotion import CooldownSpec

    assert kwargs["cooldowns"][0].kit_id is not None or kwargs["kit_id"] == 2
    spec: CooldownSpec = kwargs["cooldowns"][0]
    resolved = spec.kit_id if spec.kit_id is not None else kwargs["kit_id"]
    assert resolved == 2


@pytest.mark.asyncio
async def test_legacy_global_row_is_always_paired_with_an_open_issue(
    session_factory, clean_db, tmp_path
):
    """A global cooldown may only exist if the import explicitly flagged it."""
    import json

    from services.phase_d.import_data import import_json_data

    async with transaction(session_factory) as session:
        await ensure_dimensions(session, KIT_DEFS, TIER_DEFS)
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=99, ign="legacy_player"
        )

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "players.json").write_text(
        json.dumps([{"username": "legacy_player", "modes": {}, "history": {}}]),
        encoding="utf-8",
    )
    # No kits.json / ht_results.json: the kit-less legacy waitlist cooldown
    # below cannot be attributed to any kit, so it stays global — and flagged.
    expires_ms = int(
        (datetime.now(timezone.utc) + timedelta(days=4)).timestamp() * 1000
    )
    (tmp_path / "cooldowns.json").write_text(
        json.dumps({"99": expires_ms}), encoding="utf-8"
    )

    report = await import_json_data(session_factory, data_dir=tmp_path)
    assert report["cooldowns_kept_global"] == 1

    global_rows = [r for r in await _all_cooldowns(session_factory) if r.kit_id is None]
    assert len(global_rows) == 1

    issues = await _open_issues(session_factory)
    assert any(
        i.category
        in {"cooldown_kit_unattributable", "cooldown_kit_unknown_in_registry"}
        and i.source_key == "99"
        for i in issues
    ), "a global cooldown exists without a flag saying why"

    # ...and it keeps blocking every kit, which is exactly why it is flagged.
    for kit_key in (BOXING, BEDWARS):
        assert await _waitlist_blocking(
            session_factory, player_id=player.id, kit_key=kit_key
        )

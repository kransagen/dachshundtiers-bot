"""/linkign — propojení Discord ↔ IGN, včetně bezpečného sloučení záznamů."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from db.models import AuditLog, Cooldown, Kit, Player, Tester, TierHistory
from db.repositories.kits import ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from services.player_link import (
    LINK_ADOPTED,
    LINK_CREATED,
    LINK_MERGED,
    LINK_RENAMED,
    LINK_UNCHANGED,
    REFUSE_ALREADY_LINKED,
    REFUSE_IGN_TAKEN,
    REFUSE_INVALID_IGN,
    REFUSE_MERGE_CONFLICT,
    LinkRefused,
    link_ign,
    linked_ign,
)

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


async def _legacy(session_factory, ign="Alice"):
    """Nepropojený záznam z players.json s historií."""
    async with transaction(session_factory) as session:
        await ensure_dimensions(session, (("nethpot", "NethPot"),), (("LT2", "ladder", "LT2", 7),))
        kit = (await session.execute(select(Kit))).scalar_one()
        player = await PlayerRepository().get_or_create_by_ign(session, ign=ign)
        from db.models import TierDefinition

        tier = (await session.execute(select(TierDefinition))).scalar_one()
        session.add(TierHistory(player_id=player.id, kit_id=kit.id, tier_id=tier.id,
                                changed_at=NOW, source="migration"))
        return player.id, kit.id


async def _player(session_factory, discord_id):
    async with transaction(session_factory) as session:
        return await PlayerRepository().get_by_discord_id(session, discord_id)


async def test_new_player_is_created_and_linked(session_factory, clean_db):
    out = await link_ign(111, "Alice", session_factory=session_factory)
    assert out.status == LINK_CREATED
    assert await linked_ign(111, session_factory=session_factory) == "Alice"


async def test_adopts_unclaimed_legacy_record_with_history(session_factory, clean_db):
    legacy_id, _ = await _legacy(session_factory)
    out = await link_ign(111, "alice", session_factory=session_factory)
    assert out.status == LINK_ADOPTED and out.player_id == legacy_id
    p = await _player(session_factory, 111)
    assert p.id == legacy_id and p.ign == "alice" and p.ign_linked_at is not None


async def test_shell_record_is_merged_into_legacy_record(session_factory, clean_db):
    """Přesně chyba „IGN už někdo vlastní“: tester má prázdný záznam + IGN s historií."""
    legacy_id, kit_id = await _legacy(session_factory)
    async with transaction(session_factory) as session:
        shell = await PlayerRepository().get_or_create_shell(session, discord_id=111)
        session.add(Tester(player_id=shell.id))
        session.add(Cooldown(player_id=shell.id, cooldown_type="waitlist", kit_id=kit_id,
                             expires_at=NOW + timedelta(days=3), source="result"))
        shell_id = shell.id

    out = await link_ign(111, "Alice", session_factory=session_factory)

    assert out.status == LINK_MERGED and out.merged_from == shell_id
    async with transaction(session_factory) as session:
        assert await session.get(Player, shell_id) is None
        p = await PlayerRepository().get_by_discord_id(session, 111)
        assert p.id == legacy_id
        assert await session.get(Tester, legacy_id) is not None
        cd = (await session.execute(select(Cooldown))).scalar_one()
        assert cd.player_id == legacy_id
        history = (await session.execute(
            select(func.count()).select_from(TierHistory).where(TierHistory.player_id == legacy_id)
        )).scalar_one()
        assert history == 1
        audit = (await session.execute(
            select(AuditLog).where(AuditLog.action == "ign_link")
        )).scalar_one()
        assert audit.details["merged_from_player_id"] == shell_id


async def test_merge_conflict_rolls_back_everything(session_factory, clean_db):
    legacy_id, kit_id = await _legacy(session_factory)
    async with transaction(session_factory) as session:
        shell = await PlayerRepository().get_or_create_shell(session, discord_id=111)
        for pid in (shell.id, legacy_id):
            session.add(Cooldown(player_id=pid, cooldown_type="waitlist", kit_id=kit_id,
                                 expires_at=NOW, source="result"))
        shell_id = shell.id

    with pytest.raises(LinkRefused) as exc:
        await link_ign(111, "Alice", session_factory=session_factory)
    assert exc.value.code == REFUSE_MERGE_CONFLICT
    async with transaction(session_factory) as session:
        assert await session.get(Player, shell_id) is not None
        legacy = await session.get(Player, legacy_id)
        assert legacy.discord_id is None and legacy.ign_linked_at is None


async def test_ign_of_another_discord_is_refused(session_factory, clean_db):
    await link_ign(222, "Alice", session_factory=session_factory)
    with pytest.raises(LinkRefused) as exc:
        await link_ign(111, "Alice", session_factory=session_factory)
    assert exc.value.code == REFUSE_IGN_TAKEN


async def test_linked_player_can_rename_to_free_ign(session_factory, clean_db):
    await link_ign(111, "Alice", session_factory=session_factory)
    out = await link_ign(111, "Alice2", session_factory=session_factory)
    assert out.status == LINK_RENAMED
    assert (await link_ign(111, "Alice2", session_factory=session_factory)).status == LINK_UNCHANGED


async def test_linked_player_cannot_take_over_other_history(session_factory, clean_db):
    await _legacy(session_factory, ign="Bob")
    await link_ign(111, "Alice", session_factory=session_factory)
    with pytest.raises(LinkRefused) as exc:
        await link_ign(111, "Bob", session_factory=session_factory)
    assert exc.value.code == REFUSE_ALREADY_LINKED


@pytest.mark.parametrize("ign", ["", "ab", "a" * 17, "bad name", "čau"])
async def test_invalid_ign_is_refused(session_factory, clean_db, ign):
    with pytest.raises(LinkRefused) as exc:
        await link_ign(111, ign, session_factory=session_factory)
    assert exc.value.code == REFUSE_INVALID_IGN


async def test_claim_never_renames_linked_player(session_factory, clean_db):
    """IGN napsané testerem v /result propojeného hráče nepřejmenuje."""
    await link_ign(111, "Alice", session_factory=session_factory)
    async with transaction(session_factory) as session:
        _status, p = await PlayerRepository().claim_discord_id(
            session, discord_id=111, ign="Alcie"
        )
    assert p.ign == "Alice"

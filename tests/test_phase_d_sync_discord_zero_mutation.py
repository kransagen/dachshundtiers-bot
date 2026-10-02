"""Phase D production regression: /sync discord je observe-only.

Důkaz, že náhled + potvrzení skutečného cogu (``_run_discord`` + tlačítko):
  - nikdy nezavolá rolovou mutaci (add_roles/remove_roles/edit_role/set_roles),
  - zrcadlí Discord → PostgreSQL (nikdy DB → Discord),
  - nikdy nepoužije players.json ke zjištění žádaných rolí.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from unittest import mock

from sqlalchemy import select

import storage
from cogs import sync as sync_cog
from db.models import Kit, TierDefinition
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.sync_audit import SyncRunRepository
from db.repositories.tiers import MirrorRepository, MirrorServiceRepository
from db.services.session import transaction


@dataclass
class SpyMember:
    id: int
    role_ids: tuple[int, ...]
    add_roles: mock.AsyncMock = field(default_factory=mock.AsyncMock)
    remove_roles: mock.AsyncMock = field(default_factory=mock.AsyncMock)
    edit_role: mock.AsyncMock = field(default_factory=mock.AsyncMock)
    set_roles: mock.AsyncMock = field(default_factory=mock.AsyncMock)


MUTATION_API = ("add_roles", "remove_roles", "edit_role", "set_roles")


async def _seed(session_factory, *, t2_role_id=7777, t1_role_id=7778):
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (("t1", "ladder", "Tier 1", 1), ("t2", "ladder", "Tier 2", 2)),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        t1 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t1")
            )
        ).scalar_one()
        t2 = (
            await session.execute(
                select(TierDefinition).where(TierDefinition.code == "t2")
            )
        ).scalar_one()
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t2.id, discord_role_id=t2_role_id
        )
        await KitRoleRepository().set_mapping(
            session, kit_id=kit.id, tier_id=t1.id, discord_role_id=t1_role_id
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign="Discord"
        )
        return {"kit": kit, "t1": t1, "t2": t2, "player": player,
                "t2_role_id": t2_role_id, "t1_role_id": t1_role_id}


async def _run_cog(session_factory, members):
    cm = sync_cog
    bot = mock.MagicMock()
    bot.db_session_factory = session_factory
    cog = cm.Sync(bot)
    inter = mock.MagicMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup.send = mock.AsyncMock()
    with mock.patch.object(
        cm, "admin_gate_error", return_value=None
    ), mock.patch.object(
        cm, "guild_members", new=mock.AsyncMock(return_value=members)
    ), mock.patch.object(
        storage, "load_data", new=mock.MagicMock(
            side_effect=AssertionError("players.json nesmí být zdrojem rolí")
        )
    ) as load_mock:
        await cog._run_discord(inter)
        view = inter.followup.send.await_args.kwargs.get("view")
        assert view is not None, "změny k zápisu musí nabídnout potvrzení"
        click = mock.MagicMock()
        click.client.db_session_factory = session_factory
        click.guild = inter.guild
        click.user.id = 1
        click.response.defer = mock.AsyncMock()
        click.followup.send = mock.AsyncMock()
        click.message.edit = mock.AsyncMock()
        await view.confirm.callback(click)
    return inter, load_mock


async def test_sync_discord_never_mutates_roles_and_discord_wins(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            tier_id=seeded["t1"].id,
            discord_role_id=seeded["t1_role_id"],
            observed_at=datetime.now(timezone.utc),
            source="promotion",
        )
    member = SpyMember(id=1111, role_ids=(seeded["t2_role_id"],))
    members = [member]

    inter, load_mock = await _run_cog(session_factory, members)

    for spy in members:
        for api in MUTATION_API:
            getattr(spy, api).assert_not_awaited()
    load_mock.assert_not_called()
    inter.followup.send.assert_awaited()

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
        runs = await SyncRunRepository().list(session, limit=1)
    assert mirror is not None
    assert mirror.tier_id == seeded["t2"].id
    assert mirror.discord_role_id == seeded["t2_role_id"]
    assert mirror.source == "discord_sync"
    assert runs, "musí vzniknout sync_run"
    assert runs[0].status == "success"


async def test_sync_discord_conflicting_stale_mirror_follows_discord(
    session_factory, clean_db
):
    seeded = await _seed(session_factory)
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session,
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
            tier_id=seeded["t1"].id,
            discord_role_id=seeded["t1_role_id"],
            observed_at=datetime.now(timezone.utc),
            source="promotion",
        )
    member = SpyMember(id=1111, role_ids=(seeded["t2_role_id"],))

    await _run_cog(session_factory, [member])

    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session, player_id=seeded["player"].id, kit_id=seeded["kit"].id
        )
    assert mirror.tier_id == seeded["t2"].id
    assert mirror.source == "discord_sync"
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
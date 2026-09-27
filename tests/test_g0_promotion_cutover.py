"""Phase G0 — H1 cutover proof.

These tests exercise the REAL, unmodified production entry points —
``Results.result`` and ``TopResult.top_result`` cog command callbacks — with
``bot.db_session_factory`` set to a real embedded-PostgreSQL session factory,
exactly as the running bot does once ``DATABASE_URL`` is configured.

The point is not "does record_result work" (test_services_results_db.py
already proves that at the service layer) — it's proving the ACTUAL Discord
command handlers, as wired today, drive the canonical DB-backed promotion
path end-to-end and never fall back to the legacy ``services.store``
JSON/JSONB transaction mechanism (H1 audit finding: they didn't, for a long
time, because ``session_factory`` was silently never passed).

Discord-side effects (role grants) are mocked — only the DB layer and the
cog's own control flow are real.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

import services.store as store_module
from db.models import PlayerCurrentTier, Result, TierHistory
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.repositories.results import PROMOTION_COMMITTED
from db.models import Kit, TierDefinition
from db.services.session import transaction as db_transaction


def _forbid_legacy_transaction(*_args, **_kwargs):
    raise AssertionError(
        "services.store.transaction (legacy JSON/JSONB path) must NEVER be "
        "called while db_session_factory is configured — this is exactly "
        "the H1 regression (players.json participating in the live "
        "promotion decision even though PostgreSQL is configured)."
    )


class _AsyncCtxOK:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def _make_guild(member):
    guild = mock.MagicMock()
    guild.get_member.side_effect = lambda mid: member if int(mid) == member.id else None
    guild.fetch_member = mock.AsyncMock(return_value=member)
    guild.get_role.side_effect = lambda rid: SimpleNamespace(
        id=rid, mention=f"<@&{rid}>", name=f"Role{rid}"
    )
    guild.channels = []
    guild.fetch_channel = mock.AsyncMock(return_value=None)
    return guild


def _make_member(discord_id: int):
    m = mock.MagicMock()
    m.id = discord_id
    m.roles = []
    m.edit = mock.AsyncMock()
    m.voice = None
    return m


class ResultCommandUsesCanonicalDbPath(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.conftest import (
            _create_fresh_database,
            _sync_url,
            _async_url,
            _alembic_config,
            EMBEDDED_PG_DIR,
        )
        from embedded_postgres import get_server
        from alembic import command
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from db.engine import create_async_engine_from_url
        from sqlalchemy.pool import NullPool

        self._server = get_server(EMBEDDED_PG_DIR, cleanup_mode="stop")
        socket_dir = str(self._server.get_postmaster_info().socket_dir)
        db_name = "pytest_g0_cutover"
        _create_fresh_database(socket_dir, db_name)
        sync_url = _sync_url(socket_dir, db_name)
        command.upgrade(_alembic_config(sync_url), "head")

        self._engine = create_async_engine_from_url(
            _async_url(socket_dir, db_name), poolclass=NullPool
        )
        self.session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def asyncTearDown(self):
        from db.engine import dispose_engine

        await dispose_engine(self._engine)
        self._server.cleanup()

    async def _seed(self):
        async with db_transaction(self.session_factory) as session:
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
            lt3 = (
                await session.execute(
                    select(TierDefinition).where(TierDefinition.code == "LT3")
                )
            ).scalar_one()
            await KitRoleRepository().set_mapping(
                session, kit_id=kit.id, tier_id=lt3.id, discord_role_id=555111
            )
            await PlayerRepository().claim_discord_id(
                session, discord_id=42424242, ign="Cutover"
            )

    def _interaction(self, member):
        inter = mock.MagicMock()
        inter.user = SimpleNamespace(
            id=777,
            name="tester",
            display_name="tester",
            roles=[SimpleNamespace(id=1, name="Tester")],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        inter.guild = _make_guild(member)
        inter.channel = mock.MagicMock()  # not a TextChannel → queue path, no ticket
        inter.response.send_message = mock.AsyncMock()
        inter.response.defer = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        return inter

    async def test_result_command_writes_db_result_never_touches_json_path(self):
        from cogs.results import Results

        await self._seed()

        cog = Results.__new__(Results)
        cog.bot = mock.MagicMock()
        cog.bot.db_session_factory = self.session_factory
        cog.bot.get_channel.return_value = None

        member = _make_member(42424242)
        inter = self._interaction(member)
        hrac = SimpleNamespace(id=42424242, name="Cutover", display_name="Cutover")

        with (
            mock.patch.object(store_module, "transaction", _forbid_legacy_transaction),
            mock.patch("cogs.results.has_tester_role", return_value=True),
        ):
            await Results.result.callback(
                cog,
                interaction=inter,
                hrac=hrac,
                ign="Cutover",
                kit="HT3",
                tier="LT3",
                score="3-1",
                outcome="Won",
            )

        # The command must have completed successfully (not bailed out on an
        # early validation error) — followup.send was called with a real
        # result embed, and response.send_message (used for early-return
        # error paths) was never used.
        inter.response.send_message.assert_not_awaited()
        inter.followup.send.assert_awaited()
        member.edit.assert_awaited_once()

        async with db_transaction(self.session_factory) as session:
            result_row = (
                await session.execute(
                    select(Result).where(Result.result_key.like("result:queue-%"))
                )
            ).scalar_one()
            mirror = (
                await session.execute(select(PlayerCurrentTier))
            ).scalars().all()
            history = (await session.execute(select(TierHistory))).scalars().all()

        self.assertEqual(result_row.promotion_status, PROMOTION_COMMITTED)
        self.assertEqual(len(mirror), 1)
        self.assertEqual(mirror[0].source, "promotion")
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].previous_tier_id)

    async def test_result_in_ht_ticket_reads_db_ticket_not_stale_json(self):
        """G0 discovery: get_ticket() without session_factory only ever
        reads the legacy JSON ticket store — in DB mode, tickets are
        created via cogs/ht3.py WITH session_factory (real DB rows), so the
        unfixed call would always see `ticket = None` and silently
        misclassify every HT3+ ticket evaluation as a plain queue result.
        This proves /result now recognizes the real DB ticket."""
        import discord as discord_module

        from services.tickets import create_ticket

        await self._seed()
        created = await create_ticket(
            channel_id=909090,
            owner_id="42424242",
            owner_name="Cutover",
            ign="Cutover",
            kit="ht3",
            target_tier="LT3",
            current_tier=None,
            eval_ok=False,
            category_id=1,
            now=1_700_000_000_000,
            session_factory=self.session_factory,
        )
        self.assertEqual(created["result"], "created")

        from cogs.results import Results as ResultsCog

        cog = ResultsCog.__new__(ResultsCog)
        cog.bot = mock.MagicMock()
        cog.bot.db_session_factory = self.session_factory
        cog.bot.get_channel.return_value = None

        member = _make_member(42424242)
        inter = self._interaction(member)
        inter.channel = mock.MagicMock(spec=discord_module.TextChannel)
        inter.channel_id = 909090

        with (
            mock.patch.object(store_module, "transaction", _forbid_legacy_transaction),
            mock.patch("cogs.results.has_tester_role", return_value=True),
        ):
            await ResultsCog.result.callback(
                cog,
                interaction=inter,
                hrac=SimpleNamespace(
                    id=42424242, name="Cutover", display_name="Cutover"
                ),
                ign="Cutover",
                kit="HT3",
                tier="LT3",
                score="3-1",
                outcome="Won",
            )

        inter.response.send_message.assert_not_awaited()
        member.edit.assert_awaited_once()

        async with db_transaction(self.session_factory) as session:
            result_row = (
                await session.execute(
                    select(Result).where(Result.result_key == "result:909090")
                )
            ).scalar_one()
            ticket_row = (
                await session.execute(
                    select(Result.ticket_channel_id).where(
                        Result.result_key == "result:909090"
                    )
                )
            ).scalar_one()
        self.assertEqual(result_row.promotion_status, PROMOTION_COMMITTED)
        self.assertEqual(ticket_row, 909090)
        self.assertEqual(result_row.kind, "ticket")


if __name__ == "__main__":
    unittest.main()

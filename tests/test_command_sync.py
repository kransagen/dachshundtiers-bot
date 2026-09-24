"""Testy jednoscopové, deterministické synchronizace slash příkazů.

Pokrývají zadání:
- registrace příkazu proběhne přesně jednou (tree to vynucuje,
  ``sync_commands`` nic nepřidává),
- globální a guild scope se nikdy neplní současně,
- ``copy_global_to`` nevytváří duplicitní registrace (a je idempotentní),
- migrace bezpečně odstraní JEN obsolete guild příkazy téhle aplikace
  (globální scope se nedotýká, cizí aplikace se nedotýká),
- startovní sync je idempotentní a běží právě jednou za proces (guard),
- selhání fetche při migraci bot neshodí.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from discord.app_commands import Command, CommandAlreadyRegistered, CommandTree

import bot as bot_module
import cogs.info as info_module


async def _cb(interaction):
    pass


def _cmd(name):
    return Command(name=name, description="x", callback=_cb)


class _FakeClient:
    """Client pro reálný CommandTree (bez sítě)."""

    http = mock.MagicMock()
    application_id = 999
    tree = None
    _connection = mock.MagicMock()


_FakeClient._connection._command_tree = None


def _make_client():
    """Čerstvý fake client – každý reálný CommandTree si bere VLASTNÍ instanci.

    Sdílený ``_connection`` mezi testy by druhý ``CommandTree()`` zrušil
    (``_command_tree`` už by nebyl None → ClientException).
    """
    client = mock.MagicMock()
    client._connection._command_tree = None
    return client


def _mock_tree(*, local=("result", "sync"), guild_cmds=()):
    """Tree se zamockovanou HTTP vrstvou – testuje orchestraci sync_commands."""
    tree = mock.MagicMock()
    tree.get_commands.return_value = [
        SimpleNamespace(name=n) for n in local
    ]
    tree.fetch_commands = mock.AsyncMock(
        return_value=[
            SimpleNamespace(id=i, name=n) for i, n in guild_cmds
        ]
    )
    tree.sync = mock.AsyncMock(return_value=[object()])
    tree._http = mock.MagicMock()
    tree._http.delete_guild_command = mock.AsyncMock()
    tree._http.delete_global_command = mock.AsyncMock()
    tree.client = mock.MagicMock()
    tree.client.application_id = 999
    return tree


class SyncCommandsTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # Registrace proběhne přesně jednou
    # ------------------------------------------------------------------
    def test_registration_occurs_exactly_once(self):
        tree = CommandTree(client=_make_client())
        tree.add_command(_cmd("result"))
        with self.assertRaises(CommandAlreadyRegistered):
            tree.add_command(_cmd("result"))

        # sync_commands sám nic neregistruje ani neodstraňuje z tree
        async def main():
            mtree = _mock_tree(local=("result",))
            await bot_module.sync_commands(mtree, guild_id=None)
            mtree.add_command.assert_not_called()
            mtree.remove_command.assert_not_called()

        asyncio.run(main())

    # ------------------------------------------------------------------
    # Globální a guild scope se nikdy neplní současně
    # ------------------------------------------------------------------
    def test_guild_mode_syncs_only_guild(self):
        async def main():
            tree = _mock_tree(
                local=("result", "sync"),
                guild_cmds=((1, "result"), (2, "sync")),
            )
            info = await bot_module.sync_commands(tree, guild_id=777)

            self.assertEqual(info["scope"], "guild")
            tree.copy_global_to.assert_called_once_with(
                guild=discord.Object(id=777)
            )
            tree.sync.assert_awaited_once_with(guild=discord.Object(id=777))
            # žádné globální mazání/přepis
            tree._http.delete_global_command.assert_not_awaited()
            for call in tree.sync.await_args_list:
                self.assertIn("guild", call.kwargs)

        asyncio.run(main())

    def test_global_mode_syncs_only_global(self):
        async def main():
            tree = _mock_tree(local=("result", "sync"))
            info = await bot_module.sync_commands(tree, guild_id=None)

            self.assertEqual(info["scope"], "global")
            tree.sync.assert_awaited_once_with()  # bez guild
            tree.copy_global_to.assert_not_called()
            tree.fetch_commands.assert_not_awaited()
            tree._http.delete_guild_command.assert_not_awaited()

        asyncio.run(main())

    # ------------------------------------------------------------------
    # copy_global_to nevytváří duplicitní registrace
    # ------------------------------------------------------------------
    def test_copy_global_to_creates_no_duplicates(self):
        tree = CommandTree(client=_make_client())
        for name in ("result", "sync", "verze"):
            tree.add_command(_cmd(name))

        guild = discord.Object(id=777)
        tree.copy_global_to(guild=guild)

        glob = [c.name for c in tree.get_commands()]
        gld = [c.name for c in tree.get_commands(guild=guild)]
        self.assertEqual(sorted(glob), sorted(gld))
        self.assertEqual(len(glob), len(set(glob)))
        self.assertEqual(len(gld), len(set(gld)))

        # guild scope obsahuje STEJNÉ Command objekty (žádná duplicita registrace)
        guild_by_name = {c.name: c for c in tree.get_commands(guild=guild)}
        self.assertIs(guild_by_name["result"], tree.get_command("result"))

        # opakovaná kopie je idempotentní
        tree.copy_global_to(guild=guild)
        self.assertEqual(len(tree.get_commands(guild=guild)), 3)

    # ------------------------------------------------------------------
    # Migrace: smazání jen obsolete guild příkazů téhle aplikace
    # ------------------------------------------------------------------
    def test_migration_removes_only_obsolete_guild_commands(self):
        async def main():
            tree = _mock_tree(
                local=("result", "sync"),
                # result/sync mají zůstat, old_cmd + legacy jsou obsolete
                guild_cmds=((1, "result"), (2, "old_cmd"), (3, "legacy")),
            )
            info = await bot_module.sync_commands(tree, guild_id=777)

            self.assertEqual(sorted(info["removed_guild"]), ["legacy", "old_cmd"])
            deleted = [
                c.args for c in tree._http.delete_guild_command.await_args_list
            ]
            # (application_id, guild_id, command_id) – jen obsolete, jen tato aplikace
            self.assertEqual(deleted, [(999, 777, 2), (999, 777, 3)])

        asyncio.run(main())

    def test_migration_never_touches_global_or_other_scopes(self):
        async def main():
            tree = _mock_tree(
                local=("result",),
                guild_cmds=((1, "result"), (2, "old")),
            )
            await bot_module.sync_commands(tree, guild_id=777)

            tree._http.delete_global_command.assert_not_awaited()
            self.assertEqual(
                tree._http.delete_guild_command.await_args.args[1], 777
            )
            # finální sync jde jen do guildy
            tree.sync.assert_awaited_once()
            self.assertEqual(tree.sync.await_args.kwargs["guild"].id, 777)

        asyncio.run(main())

    def test_migration_fetch_failure_is_graceful(self):
        async def main():
            tree = _mock_tree(local=("result",))
            tree.fetch_commands = mock.AsyncMock(
                side_effect=discord.Forbidden(mock.MagicMock(), "no perms")
            )
            info = await bot_module.sync_commands(tree, guild_id=777)

            # fetch selhal → žádné mazání, ale sync guildy proběhne (PUT dorovná set)
            self.assertEqual(info["removed_guild"], [])
            self.assertEqual(info["synced"], 1)
            tree.copy_global_to.assert_called_once()

        asyncio.run(main())

    # ------------------------------------------------------------------
    # Startovní sync: idempotentní + guard „jednou za proces"
    # ------------------------------------------------------------------
    def test_startup_sync_runs_once(self):
        bot = bot_module.DachshundTiersBot()
        with mock.patch.object(
            bot_module,
            "sync_commands",
            new=mock.AsyncMock(
                return_value={
                    "scope": "guild",
                    "guild_id": 777,
                    "synced": 1,
                    "removed_guild": [],
                }
            ),
        ):

            async def main():
                await bot._sync_commands_once()
                await bot._sync_commands_once()

            asyncio.run(main())

            self.assertTrue(bot._commands_synced)
            self.assertEqual(bot_module.sync_commands.await_count, 1)

    def test_repeated_sync_commands_is_idempotent(self):
        async def main():
            tree = _mock_tree(
                local=("result", "sync"),
                guild_cmds=((1, "result"), (2, "sync")),
            )
            await bot_module.sync_commands(tree, guild_id=777)
            await bot_module.sync_commands(tree, guild_id=777)

            # platí jen jádro funguje bez kumulace: stejný počet volání = počet běhů
            self.assertEqual(tree.sync.await_count, 2)
            self.assertEqual(tree.copy_global_to.call_count, 2)
            # nic obsolete → nic se nikdy nemazalo
            self.assertEqual(tree._http.delete_guild_command.await_count, 0)

        asyncio.run(main())


class VerzeDiagnosticsTests(unittest.TestCase):
    """/verze rozliší lokální / Discord global / Discord guild / duplicity."""

    def test_verze_reports_scopes_and_duplicates(self):
        async def main():
            bot = mock.MagicMock()
            tree = mock.MagicMock()
            tree.get_commands.return_value = [
                SimpleNamespace(name="result", parameters=[]),
                SimpleNamespace(name="verze", parameters=[]),
            ]
            # 1. volání = globální fetch, 2. = guild fetch (pořadí v kódu)
            tree.fetch_commands = mock.AsyncMock(
                side_effect=[
                    [SimpleNamespace(name="result")],
                    [
                        SimpleNamespace(name="result"),
                        SimpleNamespace(name="sync"),
                    ],
                ]
            )
            bot.tree = tree

            inter = mock.MagicMock()
            inter.response.send_message = mock.AsyncMock()

            with mock.patch.object(info_module, "git_commit", return_value="abc1234"), \
                 mock.patch.object(info_module, "GUILD_ID", 777):
                cog = info_module.Info(bot)
                await cog.verze.callback(cog, inter)

            inter.response.send_message.assert_awaited_once()
            embed = inter.response.send_message.await_args.kwargs["embed"]
            desc = embed.description
            self.assertIn("**Slash – lokální (tree):** 2", desc)
            self.assertIn("**Slash – Discord globální:** 1", desc)
            self.assertIn("**Slash – Discord guild:** 2", desc)
            self.assertIn("**Duplicitní jména (global ∩ guild):** `result`", desc)

        asyncio.run(main())

    def test_verze_reports_no_duplicates(self):
        async def main():
            bot = mock.MagicMock()
            tree = mock.MagicMock()
            tree.get_commands.return_value = [SimpleNamespace(name="result", parameters=[])]
            tree.fetch_commands = mock.AsyncMock(
                side_effect=[
                    [SimpleNamespace(name="result")],
                    [SimpleNamespace(name="sync")],
                ]
            )
            bot.tree = tree

            inter = mock.MagicMock()
            inter.response.send_message = mock.AsyncMock()

            with mock.patch.object(info_module, "git_commit", return_value="abc1234"), \
                 mock.patch.object(info_module, "GUILD_ID", 777):
                await info_module.Info(bot).verze.callback(
                    info_module.Info(bot), inter
                )

            desc = inter.response.send_message.await_args.kwargs["embed"].description
            self.assertIn("**Duplicitní jména (global ∩ guild):** žádné", desc)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
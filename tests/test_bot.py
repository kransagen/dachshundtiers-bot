"""Testy startovacího chování bota – bot.py.

- item 7: ``_build_intents`` zapíná PRIVILEGOVANÝ Members intent (bez něj
  ``guild.get_member()`` vrací None a rozbíjí se sync rolí i lookupy),
- item 9: ``_sync_commands_once`` běží právě jednou (guard + asyncio.Lock
  proti souběžným ``on_ready``) a při SELHÁNÍ se sync NEoznačí za hotový –
  další ``on_ready`` to zkusí znovu místo tichého provozu bez příkazů.
- item 8: ``_validate_kit_role_configuration_once`` – startup validace
  kit-role mapování (design §7): 1× za běh, po přihlášení, fail-fast.
"""

import asyncio
import unittest
from unittest import mock

import bot as bot_module
from bot import _build_intents


class IntentTests(unittest.TestCase):
    def test_members_intent_enabled(self):
        """Members intent je zapnutý (privilegovaný – povinný v portálu)."""
        intents = _build_intents()
        self.assertTrue(intents.members)

    def test_other_required_intents_preserved(self):
        intents = _build_intents()
        self.assertTrue(intents.guilds)
        self.assertTrue(intents.guild_messages)

    def test_message_content_intent_disabled(self):
        self.assertFalse(_build_intents().message_content)

    def test_default_allowed_mentions_do_not_ping_everyone_or_roles(self):
        allowed = bot_module.DachshundTiersBot().allowed_mentions
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertTrue(allowed.users)


class SyncOnceTests(unittest.TestCase):
    def _make_bot(self):
        bot = bot_module.DachshundTiersBot()
        bot._commands_synced = False
        bot._sync_lock = asyncio.Lock()
        return bot

    def test_sync_runs_exactly_once_across_calls(self):
        sync = mock.AsyncMock(return_value={"scope": "global", "synced": 5})

        async def main():
            b = self._make_bot()
            with mock.patch("bot.sync_commands", sync):
                await asyncio.gather(b._sync_commands_once(), b._sync_commands_once())
                await b._sync_commands_once()
            return b

        bot = asyncio.run(main())
        self.assertEqual(sync.await_count, 1)
        self.assertTrue(bot._commands_synced)

    def test_failed_sync_is_retried_not_marked_done(self):
        """Selhání (rate limit / odpojení) → flag zůstane False → retry."""
        sync = mock.AsyncMock(
            side_effect=[RuntimeError("proxy down"), {"scope": "global", "synced": 5}]
        )

        async def main():
            b = self._make_bot()
            with mock.patch("bot.sync_commands", sync):
                await b._sync_commands_once()  # 1) selže
                self.assertFalse(b._commands_synced)
                await b._sync_commands_once()  # 2) zkusí znovu a projde
            return b

        bot = asyncio.run(main())
        self.assertEqual(sync.await_count, 2)
        self.assertTrue(bot._commands_synced)

    def test_sync_passes_guild_id(self):
        sync = mock.AsyncMock(return_value={"scope": "global", "synced": 5})

        async def main():
            b = self._make_bot()
            with mock.patch("bot.sync_commands", sync):
                await b._sync_commands_once()

        asyncio.run(main())
        self.assertIn("guild_id", sync.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()


class KitRoleValidationTests(unittest.TestCase):
    """Startup validace kit-role mapování (design §7, fail-fast)."""

    def setUp(self):
        patch = mock.patch.object(bot_module, "GUILD_ID", 1)
        patch.start()
        self.addCleanup(patch.stop)

    def _make_bot(self):
        bot = bot_module.DachshundTiersBot()
        bot._kit_role_config_validated = False
        return bot

    class _FakeTx:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *args):
            return False

    def _patch_validation(self, return_value):
        validate = mock.AsyncMock(return_value=return_value)
        patch_val = mock.patch(
            "db.services.config_validation.validate_kit_role_configuration",
            validate,
        )
        patch_tx = mock.patch(
            "db.services.session.transaction",
            lambda sf: self._FakeTx(object()),
        )
        return validate, [patch_val, patch_tx]

    async def _run(self, bot):
        await bot._validate_kit_role_configuration_once()

    def test_skipped_without_db(self):
        bot = self._make_bot()
        validate, patches = self._patch_validation(object())
        for p in patches:
            p.start()
        try:
            asyncio.run(self._run(bot))
        finally:
            for p in patches:
                p.stop()
        validate.assert_not_awaited()
        self.assertTrue(bot._kit_role_config_validated)

    def test_skipped_without_guild_id(self):
        bot = self._make_bot()
        bot.db_session_factory = object()
        validate, patches = self._patch_validation(object())
        for p in patches:
            p.start()
        try:
            with mock.patch.object(bot_module, "GUILD_ID", None):
                asyncio.run(self._run(bot))
        finally:
            for p in patches:
                p.stop()
        validate.assert_not_awaited()
        self.assertTrue(bot._kit_role_config_validated)

    def test_missing_guild_retries_next_ready(self):
        bot = self._make_bot()
        bot.db_session_factory = object()
        validate, patches = self._patch_validation(object())
        for p in patches:
            p.start()
        try:
            bot.get_guild = mock.Mock(return_value=None)
            asyncio.run(self._run(bot))
        finally:
            for p in patches:
                p.stop()
        validate.assert_not_awaited()
        self.assertFalse(bot._kit_role_config_validated)

    def test_valid_configuration_marks_done(self):
        from db.services.config_validation import KitRoleValidation

        bot = self._make_bot()
        bot.db_session_factory = object()
        guild = mock.Mock()
        guild.roles = [mock.Mock(id=5001), mock.Mock(id=5002)]
        bot.get_guild = mock.Mock(return_value=guild)
        validate, patches = self._patch_validation(
            KitRoleValidation(mapping_count=2, kits_without_mapping=())
        )
        for p in patches:
            p.start()
        try:
            with mock.patch("db.config.strict_kit_roles_enabled", lambda: False):
                asyncio.run(self._run(bot))
        finally:
            for p in patches:
                p.stop()
        validate.assert_awaited_once()
        _, kwargs = validate.await_args
        self.assertEqual(kwargs["guild_role_ids"], {5001, 5002})
        self.assertFalse(kwargs["strict_kits"])
        self.assertTrue(bot._kit_role_config_validated)

    def test_strict_env_forces_kits_without_mapping_to_error(self):
        from db.services.config_validation import KitRoleValidation

        bot = self._make_bot()
        bot.db_session_factory = object()
        guild = mock.Mock()
        guild.roles = [mock.Mock(id=6001), mock.Mock(id=6002)]
        bot.get_guild = mock.Mock(return_value=guild)
        validate, patches = self._patch_validation(
            KitRoleValidation(mapping_count=2, kits_without_mapping=())
        )
        for p in patches:
            p.start()
        try:
            with mock.patch("db.config.strict_kit_roles_enabled", lambda: True):
                asyncio.run(self._run(bot))
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(validate.await_args.kwargs["strict_kits"])

    def test_invalid_configuration_fails_fast(self):
        from db.services.config_validation import KitRoleConfigError

        bot = self._make_bot()
        bot.db_session_factory = object()
        guild = mock.Mock()
        guild.roles = [mock.Mock(id=5001)]
        bot.get_guild = mock.Mock(return_value=guild)
        bot.close = mock.AsyncMock()
        validate = mock.AsyncMock(
            side_effect=KitRoleConfigError("žádné mapování")
        )
        with mock.patch(
            "db.services.config_validation.validate_kit_role_configuration",
            validate,
        ), mock.patch(
            "db.services.session.transaction",
            lambda sf: self._FakeTx(object()),
        ):
            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(self._run(bot))
        self.assertEqual(ctx.exception.code, 1)
        bot.close.assert_awaited_once()
        self.assertFalse(bot._kit_role_config_validated)


class StartupOnceTests(unittest.TestCase):
    """``on_ready`` se volá po každém reconnectu – obnova stavu jen jednou."""

    def test_restore_state_runs_once_across_reconnects(self):
        async def main():
            b = bot_module.DachshundTiersBot()
            restore = mock.AsyncMock()
            with (
                mock.patch.object(b, "_sync_commands_once", mock.AsyncMock()),
                mock.patch.object(
                    b, "_validate_kit_role_configuration_once", mock.AsyncMock()
                ),
                mock.patch.object(b, "_restore_state", restore),
                mock.patch.object(
                    type(b), "user", new_callable=mock.PropertyMock,
                    return_value=mock.Mock(id=1),
                ),
            ):
                await b.on_ready()
                await b.on_ready()
                await b.on_ready()
            return restore

        restore = asyncio.run(main())
        self.assertEqual(restore.await_count, 1)

    def test_failed_restore_state_is_retried_on_next_ready(self):
        async def main():
            b = bot_module.DachshundTiersBot()
            restore = mock.AsyncMock(side_effect=[RuntimeError("db down"), None])
            with (
                mock.patch.object(b, "_sync_commands_once", mock.AsyncMock()),
                mock.patch.object(
                    b, "_validate_kit_role_configuration_once", mock.AsyncMock()
                ),
                mock.patch.object(b, "_restore_state", restore),
                mock.patch.object(
                    type(b), "user", new_callable=mock.PropertyMock,
                    return_value=mock.Mock(id=1),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    await b.on_ready()
                self.assertFalse(b._startup_done)
                await b.on_ready()
                await b.on_ready()
            return b, restore

        b, restore = asyncio.run(main())
        self.assertEqual(restore.await_count, 2)
        self.assertTrue(b._startup_done)

    def test_register_persistent_views_registers_tester_room_view(self):
        async def main():
            b = bot_module.DachshundTiersBot()
            added = []
            with (
                mock.patch.object(b, "add_view", side_effect=lambda v, **kw: added.append(v)),
                mock.patch("bot.get_ht3_panel", mock.AsyncMock(return_value={})),
            ):
                await b._register_persistent_views()
            return b, added

        b, added = asyncio.run(main())
        self.assertTrue(b._views_registered)
        self.assertTrue(
            any(isinstance(v, bot_module.TesterRoomView) for v in added)
        )


class MainRetryTests(unittest.TestCase):
    def _run_main(self, start_effects):
        created = []

        class FakeBot:
            def __init__(self):
                self.close = mock.AsyncMock()
                self.start = mock.AsyncMock(side_effect=start_effects.pop(0))
                created.append(self)

        sleep = mock.AsyncMock()
        engine = object()
        with (
            mock.patch.object(bot_module, "ensure_data_dir"),
            mock.patch.object(
                bot_module, "_init_database", mock.AsyncMock(return_value=(engine, object()))
            ) as init_db,
            mock.patch.object(bot_module, "DachshundTiersBot", FakeBot),
            mock.patch.object(bot_module.asyncio, "sleep", sleep),
            mock.patch("db.engine.dispose_engine", mock.AsyncMock()) as dispose,
        ):
            error = None
            try:
                asyncio.run(bot_module.main())
            except SystemExit as err:
                error = err
        return created, sleep, init_db, dispose, error

    def test_transient_errors_back_off_exponentially_and_init_db_once(self):
        created, sleep, init_db, dispose, error = self._run_main(
            [OSError("net"), OSError("net"), None]
        )
        self.assertIsNone(error)
        self.assertEqual(init_db.await_count, 1)
        self.assertEqual(len(created), 3)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [5, 10])
        for bot in created:
            bot.close.assert_awaited_once()
        dispose.assert_awaited_once()

    def test_login_failure_exits_with_code_1_without_retry(self):
        created, sleep, _init_db, dispose, error = self._run_main(
            [bot_module.discord.LoginFailure("bad token")]
        )
        self.assertEqual(error.code, 1)
        self.assertEqual(len(created), 1)
        sleep.assert_not_awaited()
        created[0].close.assert_awaited_once()
        dispose.assert_awaited_once()


class TreeErrorHandlerTests(unittest.TestCase):
    def _handle(self, error):
        inter = mock.MagicMock()
        inter.response.is_done.return_value = False
        inter.response.send_message = mock.AsyncMock()
        tree = bot_module.DachshundTiersTree.__new__(bot_module.DachshundTiersTree)
        with mock.patch.object(bot_module.log, "exception") as log_exception:
            asyncio.run(tree.on_error(inter, error))
        return inter.response.send_message.await_args.args[0], log_exception

    def test_check_failure_is_not_logged_as_exception(self):
        msg, log_exception = self._handle(bot_module.discord.app_commands.CheckFailure())
        log_exception.assert_not_called()
        self.assertIn("nemůžeš", msg)

    def test_cooldown_reports_retry_after(self):
        cooldown = bot_module.discord.app_commands.Cooldown(1, 10)
        error = bot_module.discord.app_commands.CommandOnCooldown(cooldown, 7.0)
        msg, log_exception = self._handle(error)
        log_exception.assert_not_called()
        self.assertIn("7 s", msg)

    def test_unexpected_error_is_logged(self):
        msg, log_exception = self._handle(bot_module.discord.app_commands.AppCommandError("x"))
        log_exception.assert_called_once()
        self.assertIn("neočekávaná chyba", msg)

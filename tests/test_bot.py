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
        self.assertTrue(intents.message_content)


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

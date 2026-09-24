"""Testy startovacího chování bota – bot.py.

- item 7: ``_build_intents`` zapíná PRIVILEGOVANÝ Members intent (bez něj
  ``guild.get_member()`` vrací None a rozbíjí se sync rolí i lookupy),
- item 9: ``_sync_commands_once`` běží právě jednou (guard + asyncio.Lock
  proti souběžným ``on_ready``) a při SELHÁNÍ se sync NEoznačí za hotový –
  další ``on_ready`` to zkusí znovu místo tichého provozu bez příkazů.
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
"""Testy živého panelu fronty – ořez embedu, serializace a logování chyb."""

import asyncio
import unittest
from unittest import mock

import discord

import panel


def _http_error():
    return discord.HTTPException(mock.Mock(status=400, reason="Bad Request"), "bad")


class EmbedTests(unittest.TestCase):
    def test_small_queue_is_listed_in_full(self):
        embed = panel.create_queue_embed("Kit", [{"id": 1}, {"id": 2}], [9])
        self.assertIn("1. <@1>\n2. <@2>", embed.description)
        self.assertIn("1. <@9>", embed.description)
        self.assertNotIn("dalších", embed.description)

    def test_empty_queue_message(self):
        embed = panel.create_queue_embed("Kit", [], [])
        self.assertIn("Fronta je prázdná", embed.description)

    def test_huge_queue_is_truncated_within_discord_limit(self):
        queue = [{"id": 100000000000000000 + i} for i in range(500)]
        testers = list(range(200000000000000000, 200000000000000100))
        embed = panel.create_queue_embed("Kit", queue, testers)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertIn("…a dalších", embed.description)
        self.assertIn("**Aktivní Testeři**", embed.description)


class UpdatePanelTests(unittest.TestCase):
    def _guild(self, message):
        channel = mock.Mock()
        channel.fetch_message = mock.AsyncMock(return_value=message)
        guild = mock.Mock()
        guild.get_channel.return_value = channel
        return guild

    def _patches(self, order=None):
        async def state(*a, **k):
            if order is not None:
                order.append("read")
                await asyncio.sleep(0)
            return {"name": "Kit", "testers": []}

        return (
            mock.patch.object(panel, "queue_state", state),
            mock.patch.object(panel, "list_queue_entries", mock.AsyncMock(return_value=[])),
            mock.patch.object(panel, "panel_message_id", mock.AsyncMock(return_value="5")),
            mock.patch.object(panel, "get_queue_channel_id", mock.AsyncMock(return_value=7)),
        )

    def _run(self, coro_factory, patches):
        async def main():
            for p in patches:
                p.start()
            try:
                return await coro_factory()
            finally:
                for p in patches:
                    p.stop()

        return asyncio.run(main())

    def test_edit_failure_is_logged(self):
        message = mock.Mock()
        message.edit = mock.AsyncMock(side_effect=_http_error())
        guild = self._guild(message)
        with self.assertLogs("dachshundtiers", level="WARNING") as logs:
            self._run(
                lambda: panel.update_panel(guild, "kit", session_factory=None),
                self._patches(),
            )
        self.assertIn("úprava zprávy selhala", logs.output[0])

    def test_concurrent_updates_are_serialized(self):
        events = []

        async def edit(**kwargs):
            events.append("edit-start")
            await asyncio.sleep(0.01)
            events.append("edit-end")

        message = mock.Mock()
        message.edit = edit
        guild = self._guild(message)

        async def both():
            await asyncio.gather(
                panel.update_panel(guild, "kit", session_factory=None),
                panel.update_panel(guild, "kit", session_factory=None),
            )

        self._run(both, self._patches(events))
        self.assertEqual(events, ["read", "edit-start", "edit-end"] * 2)

    def test_missing_message_is_ignored_silently(self):
        guild = mock.Mock()
        channel = mock.Mock()
        channel.fetch_message = mock.AsyncMock(
            side_effect=discord.NotFound(mock.Mock(status=404, reason="nf"), "nf")
        )
        guild.get_channel.return_value = channel
        self._run(
            lambda: panel.update_panel(guild, "kit", session_factory=None),
            self._patches(),
        )


if __name__ == "__main__":
    unittest.main()

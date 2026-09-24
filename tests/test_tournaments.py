"""Regresní testy /turnajresult (cogs/tournaments.py).

- /turnajresult pinguje POUZE nakonfigurovanou TOP_RESULT_ROLE_ID
  (AllowedMentions everyone=False, roles=[role]), nikdy @everyone –
  stejný vzor jako /topresult,
- chybějící TOP_RESULT_ROLE_ID → jasná konfigurační chyba, nic se neposílá.
"""

import asyncio
import unittest
from unittest import mock

import discord

import cogs.tournaments as tournaments

ROLE_ID = 1523984977371594772
CHANNEL_ID = 1505130493283405884


def _guild(role_found: bool = True):
    guild = mock.MagicMock()
    if role_found:
        role = mock.MagicMock(id=ROLE_ID, name="Top Výsledky")
        guild.get_role.return_value = role
    else:
        guild.get_role.return_value = None
    return guild


def _interaction(guild, channel):
    inter = mock.MagicMock()
    inter.guild = guild
    inter.user.name = "zapisovatel"
    inter.response.send_message = mock.AsyncMock()
    guild.get_channel.return_value = channel
    return inter


def _player():
    p = mock.MagicMock()
    p.id = 1419031701920940163
    return p


class TournamentResultPingTests(unittest.TestCase):
    def setUp(self):
        self._patches = [
            mock.patch.object(tournaments, "TOP_RESULT_ROLE_ID", ROLE_ID),
            mock.patch.object(tournaments, "TOURNAMENT_RESULT_CHANNEL_ID", CHANNEL_ID),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_pings_top_result_role_not_everyone(self):
        async def main():
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock())
            inter = _interaction(_guild(), channel)

            cog = tournaments.Tournaments(mock.MagicMock())
            await cog.turnajresult.callback(
                cog,
                interaction=inter, kit="AnchorPvP", hrac=_player(),
                z_tieru="LT3", na_tier="HT3",
            )

            channel.send.assert_awaited_once()
            kwargs = channel.send.await_args.kwargs
            self.assertEqual(kwargs["content"], f"<@&{ROLE_ID}>")
            allowed = kwargs["allowed_mentions"]
            self.assertIsInstance(allowed, discord.AllowedMentions)
            self.assertFalse(allowed.everyone)              # žádný @everyone ping
            self.assertEqual([r.id for r in allowed.roles], [ROLE_ID])
            self.assertNotIn("everyone", (kwargs["content"] or "").lower())

        asyncio.run(main())

    def test_missing_role_aborts_with_clear_error(self):
        async def main():
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock()
            inter = _interaction(_guild(role_found=False), channel)

            cog = tournaments.Tournaments(mock.MagicMock())
            await cog.turnajresult.callback(
                cog,
                interaction=inter, kit="AnchorPvP", hrac=_player(),
                z_tieru="LT3", na_tier="HT3",
            )

            # role nenalezena → jasná admin chyba a nic se neposlalo
            inter.response.send_message.assert_awaited_once()
            msg = inter.response.send_message.await_args.args[0]
            self.assertIn("TOP_RESULT_ROLE_ID", msg)
            channel.send.assert_not_awaited()

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
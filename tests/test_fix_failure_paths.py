"""Failure-path testy P0 fixů (F3 / F4) na view vrstvě (views.py).

F3 – queue pull ztráta dat: Discord selhání NESMÍ ztratit hráče (buď je
ve frontě, nebo zaznamenaný jako vytažený) a DB se mění PŘED Discord
vedlejšími efekty.
F4 – HT3 sirotek / duch: selhání vytvoření ticketu v DB smaže kanál;
selhání uvítací zprávy smaže kanál i záznam ticketu (žádný duch).

Bez reálného Discordu: interaction/guild/kanál jsou MagicMock/AsyncMock,
storage.data_data ukazuje na tempdir.
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import discord

import storage
import views
from services import queue_service, tickets


def _player_entry(uid="1", kit="AnchorPvP"):
    return queue_service.make_entry(uid, "a", "A", kit, 1_700_000_000_000)


def _set_inputs(modal, ign: str, tier: str) -> None:
    modal.ign_input._value = ign
    modal.tier_input._value = tier


def _patch(tc: unittest.TestCase, target, new) -> None:
    patcher = mock.patch.object(views, target, new)
    patcher.start()
    tc.addCleanup(patcher.stop)


def _interaction(guild=None):
    inter = mock.MagicMock()
    inter.guild = guild or mock.MagicMock()
    inter.user.id = 111
    inter.user.name = "alice"
    inter.user.display_name = "alice"
    inter.response.defer = mock.AsyncMock()
    inter.response.send_message = mock.AsyncMock()
    inter.followup.send = mock.AsyncMock()
    return inter


class GrantPullAccessFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data("queue.json", [_player_entry()])
        storage.save_data("active_queues.json", {})
        self.update_panel = mock.AsyncMock()
        _patch(self, "update_panel", self.update_panel)

    def _failing_channel(self):
        guild = mock.MagicMock()
        guild.get_channel.return_value = None
        guild.fetch_channel = mock.AsyncMock(
            side_effect=discord.NotFound(mock.MagicMock(status=404, reason="nope"), "nope")
        )
        return guild

    def test_pull_channel_missing_keeps_player_in_queue(self):
        """F3: nenalezená roomka → hráč ZŮSTÁVÁ ve frontě, žádný ztracený pull."""

        async def main():
            guild = self._failing_channel()
            inter = _interaction(guild)
            await views.grant_pull_access(inter, 123, "AnchorPvP", kit_key="anchorpvp")

            inter.response.send_message.assert_awaited_once()
            msg = inter.response.send_message.await_args.args[0]
            self.assertIn("Hráč zůstává ve frontě", msg)
            # fronta nedotčená, žádný pulled záznam, žádný Discord write
            queue = storage.load_data("queue.json")
            self.assertEqual([p["id"] for p in queue], ["1"])
            self.assertEqual(storage.load_data("pulled_players.json", {}), {})
            self.update_panel.assert_not_awaited()

        asyncio.run(main())

    def test_pull_permission_failure_still_records_player(self):
        """F3: selhání práv → hráč už je ATOMICky vytažený (ne ztracený)."""

        async def main():
            guild = mock.MagicMock()
            channel = mock.MagicMock()
            channel.id = 777
            channel.set_permissions = mock.AsyncMock(
                side_effect=discord.Forbidden(
                    mock.MagicMock(status=403, reason="no"), "no"
                )
            )
            channel.send = mock.AsyncMock()
            guild.get_channel.return_value = channel
            inter = _interaction(guild)

            await views.grant_pull_access(inter, 777, "AnchorPvP", kit_key="anchorpvp")

            # hráč je vytažený a zaznamenaný – ne visí nikde napůl
            self.assertEqual(storage.load_data("queue.json"), [])
            pulled = storage.load_data("pulled_players.json", {})
            self.assertEqual(pulled["1"]["channel"], "777")
            msg = inter.response.send_message.await_args.args[0]
            self.assertIn("⚠️", msg)
            self.assertIn("práva se nepovedlo udělit", msg)
            self.update_panel.assert_awaited_once()

        asyncio.run(main())


class HT3ModalFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            (tickets.HT_TICKETS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
        ):
            storage.save_data(name, default)
        # orchestrátorské patch: netestujeme tier/eval/overwrite logiku
        _patch(self, "find_player_tier", mock.Mock(return_value="LT3"))
        _patch(self, "has_eval", mock.Mock(return_value=True))
        _patch(self, "find_open_ticket", mock.AsyncMock(return_value=None))
        _patch(self, "get_ht3_ticket_category", mock.Mock(return_value=777))
        _patch(self, "_apply_ticket_overwrites", mock.Mock(return_value={}))

    def _modal_interaction(self, channel):
        guild = mock.MagicMock()
        guild.get_channel.side_effect = lambda cid: mock.MagicMock() if cid == 777 else None
        guild.create_text_channel = mock.AsyncMock(return_value=channel)
        return _interaction(guild)

    def test_on_submit_create_failure_deletes_channel(self):
        """F4: selhání zápisu ticketu po vytvoření kanálu → kanál se smaže."""

        async def main():
            channel = mock.MagicMock()
            channel.id = 123
            channel.delete = mock.AsyncMock()
            _patch(self, 
                "create_ticket", mock.AsyncMock(side_effect=RuntimeError("db down"))
            )

            modal = views.HT3Modal("AnchorPvP")
            _set_inputs(modal, "AliceMC", "HT3")
            inter = self._modal_interaction(channel)

            with self.assertRaises(RuntimeError):
                await modal.on_submit(inter)
            channel.delete.assert_awaited_once()
            self.assertEqual(storage.load_data(tickets.HT_TICKETS_FILE, {}), {})

        asyncio.run(main())

    def test_on_submit_send_failure_removes_ticket(self):
        """F4: selhání uvítací zprávy → kanál i záznam ticketu se smažou."""

        async def main():
            channel = mock.MagicMock()
            channel.id = 123
            channel.delete = mock.AsyncMock()
            channel.send = mock.AsyncMock(
                side_effect=discord.Forbidden(
                    mock.MagicMock(status=403, reason="no"), "no"
                )
            )
            storage.save_data(tickets.HT_TICKETS_FILE, {"123": {"id": "123"}})
            _patch(self, 
                "create_ticket",
                mock.AsyncMock(
                    return_value={"result": "created", "ticket": {"id": "123"}}
                ),
            )
            _patch(self, "ticket_embed", mock.Mock(return_value=mock.MagicMock()))

            modal = views.HT3Modal("AnchorPvP")
            _set_inputs(modal, "AliceMC", "HT3")
            inter = self._modal_interaction(channel)

            await modal.on_submit(inter)

            # kompenzace: kanál smazaný + žádný duch v DB
            channel.delete.assert_awaited_once()
            self.assertEqual(storage.load_data(tickets.HT_TICKETS_FILE, {}), {})
            msg = inter.followup.send.await_args.args[0]
            self.assertIn("kanál byl smazán", msg)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
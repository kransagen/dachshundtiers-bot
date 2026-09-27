"""JSON režim (session_factory=None) nových front-životního-cyklu služeb — Phase F.

Ověřuje, že zadní cesta zachovává původní JSON kontrakt (active_queues.json,
queue.json, pulled_players.json, testers.json, queue_messages.json) pro
open/close fronty, joinasqueue/leaveq a /skip. DB režim (parita) je pokryt
test_services_queue_lifecycle_db.py.
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import storage
from services import queue_service as qsvc

COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000


class QueueLifecycleJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

        storage.save_data("active_queues.json", {})
        storage.save_data("queue.json", [])
        storage.save_data("queue_messages.json", {})
        storage.save_data("testers.json", [])
        storage.save_data("cooldowns.json", {})
        storage.save_data("pulled_players.json", {})

    def test_open_queue_json(self):
        asyncio.run(self._open_ok())

    async def _open_ok(self):
        status, qdata = await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
        self.assertEqual(status, "ok")
        self.assertEqual(qdata["name"], "AnchorPvP")
        self.assertEqual(qdata["opener"], "111")
        self.assertEqual(qdata["testers"], ["111"])
        self.assertIsInstance(qdata["time"], int)

        status, _ = await qsvc.open_queue("anchorpvp", "AnchorPvP", "222", "Second")
        self.assertEqual(status, "exists")

        active = storage.load_data("active_queues.json", {})
        self.assertIn("anchorpvp", active)

    def test_close_queue_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            await qsvc.join_queue(
                "1", "alice", "AliceMC", "AnchorPvP",
                joined_at_ms=1_700_000_000_000, cooldown_ms=COOLDOWN_MS,
            )
            old = await qsvc.close_queue("anchorpvp")
            self.assertIsNone(old)
            state = await qsvc.queue_state("anchorpvp")
            self.assertIsNone(state)
            entries = await qsvc.list_queue_entries("anchorpvp")
            self.assertEqual(entries, [])
        asyncio.run(run())

    def test_join_and_leave_tester_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            status, qdata = await qsvc.join_queue_tester("anchorpvp", "222")
            self.assertEqual(status, "ok")
            self.assertEqual(qdata["testers"], ["111", "222"])

            status, _ = await qsvc.join_queue_tester("anchorpvp", "222")
            self.assertEqual(status, "duplicate")

            status, qdata = await qsvc.leave_queue_tester("anchorpvp", "111")
            self.assertEqual(status, "ok")
            self.assertEqual(qdata["opener"], "222")

            status, _ = await qsvc.leave_queue_tester("anchorpvp", "111")
            self.assertEqual(status, "not_listed")

            status, _ = await qsvc.join_queue_tester("molepvp", "333")
            self.assertEqual(status, "closed")
        asyncio.run(run())

    def test_skip_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            await qsvc.join_queue(
                "1", "alice", "AliceMC", "AnchorPvP",
                joined_at_ms=1_700_000_000_000, cooldown_ms=COOLDOWN_MS,
            )
            await qsvc.join_queue(
                "2", "bob", "BobMC", "AnchorPvP",
                joined_at_ms=1_700_000_000_100, cooldown_ms=COOLDOWN_MS,
            )
            first = await qsvc.peek_first_player()
            self.assertEqual(first["id"], "1")
            result = await qsvc.skip_player("1")
            self.assertFalse(result["was_pulled"])
            self.assertFalse(result["requeued"])
            self.assertTrue(result["moved"])
            self.assertEqual(result["next"]["id"], "2")
            entries = await qsvc.list_queue_entries("anchorpvp")
            self.assertEqual([e["id"] for e in entries], ["1", "2"])
        asyncio.run(run())

    def test_skip_pulled_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            player = qsvc.make_entry("1", "alice", "AliceMC", "AnchorPvP", 1_700_000_000_000)
            await qsvc.save_pulled_player(player, 555)
            result = await qsvc.skip_player("1")
            self.assertTrue(result["was_pulled"])
            self.assertEqual(result["channel_id"], 555)
            self.assertEqual(result["kit_key"], "anchorpvp")
        asyncio.run(run())

    def test_removeq_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            await qsvc.join_queue(
                "1", "alice", "AliceMC", "AnchorPvP",
                joined_at_ms=1_700_000_000_000, cooldown_ms=COOLDOWN_MS,
            )
            entry = await qsvc.removeq("1")
            self.assertEqual(entry["id"], "1")
            self.assertIsNone(await qsvc.removeq("1"))
        asyncio.run(run())

    def test_register_global_tester_json(self):
        async def run():
            self.assertTrue(await qsvc.register_global_tester("777", "Seven"))
            self.assertTrue(await qsvc.register_global_tester("777", "Seven"))
            testers = storage.load_data("testers.json", [])
            self.assertEqual(testers, ["777"])
        asyncio.run(run())

    def test_panel_message_id_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            await qsvc.set_queue_panel("anchorpvp", 555, 999)
            mid = await qsvc.panel_message_id("anchorpvp")
            self.assertEqual(mid, "999")
        asyncio.run(run())

    def test_snapshot_json(self):
        async def run():
            await qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener")
            await qsvc.join_queue(
                "1", "alice", "AliceMC", "AnchorPvP",
                joined_at_ms=1_700_000_000_000, cooldown_ms=COOLDOWN_MS,
            )
            entries, active = await qsvc.queue_snapshot()
            self.assertEqual([e["id"] for e in entries], ["1"])
            self.assertIn("anchorpvp", active)
        asyncio.run(run())


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
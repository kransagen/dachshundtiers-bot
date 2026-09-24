"""Testy servisní logiky front services/queue_service.py (bez discord.py).

Na rozdíl od produkce běží testy v izolovaném ``data`` adresáři (tempdir),
ať nesahají na reálná data bota.
"""

import asyncio
import tempfile
import time
import unittest
from unittest import mock

import storage
from services import queue_service

COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000  # 4 dny, stejně jako v config


def _ms():
    return time.time() * 1000


class QueueServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

        storage.save_data(
            "active_queues.json",
            {"anchorpvp": {"name": "AnchorPvP", "opener": "99", "testers": ["99"]}},
        )
        storage.save_data("queue.json", [])
        storage.save_data("cooldowns.json", {})

    async def _join(self, uid, name="alice", ign="AliceMC", kit="AnchorPvP", at=None):
        return await queue_service.join_queue(
            uid,
            name,
            ign,
            kit,
            joined_at_ms=at if at is not None else _ms(),
            cooldown_ms=COOLDOWN_MS,
        )

    def test_join_success(self):
        async def main():
            result = await self._join("1")
            self.assertEqual(result["result"], "joined")

            queue = storage.load_data("queue.json")
            self.assertEqual(len(queue), 1)
            entry = queue[0]
            self.assertEqual(entry["id"], "1")
            self.assertEqual(entry["kit"], "AnchorPvP")
            self.assertEqual(entry["username"], "alice")

        asyncio.run(main())

    def test_join_closed_queue(self):
        async def main():
            storage.save_data("active_queues.json", {})
            result = await self._join("1")
            self.assertEqual(result["result"], "closed")
            self.assertEqual(storage.load_data("queue.json"), [])

        asyncio.run(main())

    def test_join_cooldown_blocks(self):
        async def main():
            now = _ms()
            storage.save_data("cooldowns.json", {"1": now})
            result = await self._join("1", at=now + 1000)
            self.assertEqual(result["result"], "cooldown")
            self.assertGreater(result["remaining"], 0)
            self.assertEqual(storage.load_data("queue.json"), [])

        asyncio.run(main())

    def test_join_after_cooldown_passes(self):
        async def main():
            now = _ms()
            storage.save_data("cooldowns.json", {"1": now})
            result = await self._join("1", at=now + COOLDOWN_MS + 1)
            self.assertEqual(result["result"], "joined")

        asyncio.run(main())

    def test_join_duplicate_blocked(self):
        async def main():
            storage.save_data(
                "queue.json",
                [queue_service.make_entry("1", "alice", "AliceMC", "AnchorPvP", _ms())],
            )
            result = await self._join("1")
            self.assertEqual(result["result"], "duplicate")
            self.assertEqual(len(storage.load_data("queue.json")), 1)

        asyncio.run(main())

    def test_concurrent_joins_same_user_no_duplicate(self):
        """Dvě souběžné interakce stejného hráče → jen jeden záznam."""

        async def main():
            results = await asyncio.gather(self._join("1"), self._join("1"))
            statuses = sorted(r["result"] for r in results)
            self.assertEqual(statuses, ["duplicate", "joined"])
            self.assertEqual(len(storage.load_data("queue.json")), 1)

        asyncio.run(main())

    def test_concurrent_joins_two_users_both_land(self):
        """Dvě souběžné interakce dvou hráčů → žádný ztracený zápis."""

        async def main():
            async def join(uid, name, ign):
                return await queue_service.join_queue(
                    uid, name, ign, "AnchorPvP",
                    joined_at_ms=_ms(), cooldown_ms=COOLDOWN_MS,
                )

            results = await asyncio.gather(join("1", "a", "A"), join("2", "b", "B"))
            self.assertEqual(sorted(r["result"] for r in results), ["joined", "joined"])
            self.assertEqual(len(storage.load_data("queue.json")), 2)

        asyncio.run(main())

    def test_leave_queue(self):
        async def main():
            storage.save_data(
                "queue.json",
                [
                    queue_service.make_entry("1", "a", "A", "AnchorPvP", _ms()),
                    queue_service.make_entry("2", "b", "B", "MolePVP", _ms()),
                ],
            )
            self.assertTrue(await queue_service.leave_queue("1", "AnchorPvP"))
            queue = storage.load_data("queue.json")
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["id"], "2")

            # podruhé už není – a cizí kit se nesahá
            self.assertFalse(await queue_service.leave_queue("1", "AnchorPvP"))
            self.assertEqual(len(storage.load_data("queue.json")), 1)

        asyncio.run(main())

    def test_pop_for_kit_pops_first_of_kit(self):
        async def main():
            storage.save_data(
                "queue.json",
                [
                    queue_service.make_entry("1", "a", "A", "MolePVP", _ms()),
                    queue_service.make_entry("2", "b", "B", "AnchorPvP", _ms()),
                    queue_service.make_entry("3", "c", "C", "AnchorPvP", _ms()),
                ],
            )
            first = await queue_service.pop_for_kit("anchorpvp")
            self.assertEqual(first["id"], "2")
            self.assertEqual(
                [p["id"] for p in storage.load_data("queue.json")], ["1", "3"]
            )

            second = await queue_service.pop_for_kit("anchorpvp")
            self.assertEqual(second["id"], "3")
            self.assertEqual(
                [p["id"] for p in storage.load_data("queue.json")], ["1"]
            )

            none = await queue_service.pop_for_kit("anchorpvp")
            self.assertIsNone(none)

        asyncio.run(main())

    def test_remove_by_player_id(self):
        async def main():
            storage.save_data(
                "queue.json",
                [queue_service.make_entry("1", "a", "A", "AnchorPvP", _ms())],
            )
            self.assertTrue(await queue_service.remove_by_player_id("1"))
            self.assertFalse(await queue_service.remove_by_player_id("1"))
            self.assertEqual(storage.load_data("queue.json"), [])

        asyncio.run(main())

    def test_pulled_player_roundtrip(self):
        async def main():
            player = queue_service.make_entry("7", "g", "Guy", "AnchorPvP", _ms())
            await queue_service.save_pulled_player(player, 123456)
            pulled = storage.load_data("pulled_players.json", {})
            self.assertIn("7", pulled)
            self.assertEqual(pulled["7"]["channel"], "123456")
            self.assertEqual(pulled["7"]["player"]["ign"], "Guy")

            self.assertTrue(await queue_service.remove_pulled_player("7"))
            self.assertEqual(storage.load_data("pulled_players.json", {}), {})

        asyncio.run(main())


class CooldownRemainingTests(unittest.TestCase):
    """cooldowns.json ukládá čas POSLEDNÍHO testu (ne expiry).

    Semantika je sdílená join flow (/queue) i /cooldown zobrazením –
    „kolik zbývá" = last_test + cooldown_ms - now, ne last_test - now.
    """

    COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000

    def test_remaining_inside_window(self):
        now = 1_000_000
        remaining = queue_service.cooldown_remaining(
            {"1": now - 60_000}, "1", now, self.COOLDOWN_MS
        )
        self.assertEqual(remaining, self.COOLDOWN_MS - 60_000)

    def test_expired_returns_none(self):
        now = 1_000_000
        remaining = queue_service.cooldown_remaining(
            # „starý" timestamp – test proběhl dávno, cooldown vypršel
            {"1": now - self.COOLDOWN_MS - 1},
            "1",
            now,
            self.COOLDOWN_MS,
        )
        self.assertIsNone(remaining)

    def test_no_record_returns_none(self):
        now = 1_000_000
        self.assertIsNone(
            queue_service.cooldown_remaining({}, "1", now, self.COOLDOWN_MS)
        )

    def test_other_player_unaffected(self):
        now = 1_000_000
        remaining = queue_service.cooldown_remaining(
            {"2": now - 60_000}, "1", now, self.COOLDOWN_MS
        )
        self.assertIsNone(remaining)


if __name__ == "__main__":
    unittest.main()
"""Testy transakčního úložiště services/store.py (bez discord.py)."""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

import storage
from services import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_read_write_roundtrip(self):
        async def main():
            await store.write("a.json", {"x": 1})
            self.assertEqual(await store.read("a.json", {}), {"x": 1})

        asyncio.run(main())

    def test_transaction_commit(self):
        async def main():
            async def fn(tx):
                data = tx.get("a.json", {})
                data["hit"] = True
                tx.set("a.json", data)
                return "done"

            result = await store.transaction(("a.json",), fn)
            self.assertEqual(result, "done")
            self.assertEqual(await store.read("a.json", {}), {"hit": True})

        asyncio.run(main())

    def test_transaction_supports_sync_fn(self):
        async def main():
            def fn(tx):
                tx.set("a.json", [1, 2, 3])
                return 7

            result = await store.transaction(("a.json",), fn)
            self.assertEqual(result, 7)
            self.assertEqual(storage.load_data("a.json"), [1, 2, 3])

        asyncio.run(main())

    def test_transaction_rollback_on_error(self):
        async def main():
            await store.write("a.json", {"v": 1})

            async def fn(tx):
                tx.set("a.json", {"v": 2})
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                await store.transaction(("a.json",), fn)
            self.assertEqual(storage.load_data("a.json"), {"v": 1})

        asyncio.run(main())

    def test_transaction_aborts_on_corrupt_file(self):
        async def main():
            with open(
                os.path.join(self._tmp, "f.json"), "w", encoding="utf-8"
            ) as f:
                f.write("{not json")

            async def fn(tx):
                data = tx.get("f.json", {})
                tx.set("f.json", dict(data, written=True))
                return "never"

            # korupce se NIKDY neopraví přepsáním – transakce se přeruší
            with self.assertRaises(storage.DataCorruptionError):
                await store.transaction(("f.json",), fn)
            with open(os.path.join(self._tmp, "f.json"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "{not json")

        asyncio.run(main())

    def test_concurrent_transactions_serialize(self):
        """Souběžné transakce nad stejným souborem se nesmějí ztrácet."""

        async def main():
            await store.write("counter.json", {"n": 0})

            async def increment():
                async def fn(tx):
                    data = tx.get("counter.json", {"n": 0})
                    await asyncio.sleep(0.005)  # šance na prohození bez zámku
                    data["n"] += 1
                    tx.set("counter.json", data)

                await store.transaction(("counter.json",), fn)

            await asyncio.gather(*(increment() for _ in range(5)))
            self.assertEqual((await store.read("counter.json"))["n"], 5)

        asyncio.run(main())

    def test_loop_scoped_locks_survive_multiple_runs(self):
        """asyncio.Lock nesmí být „bound to a different event loop"
        mezi opakovanými asyncio.run() (testy i bot pracují stejně)."""

        async def main():
            async def fn(tx):
                tx.set("x.json", [2])

            await store.transaction(("x.json",), fn)

        asyncio.run(main())
        asyncio.run(main())  # nesmí vyhodit RuntimeError
        self.assertEqual(storage.load_data("x.json"), [2])


if __name__ == "__main__":
    unittest.main()
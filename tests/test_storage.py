"""Testy atomického zápisu a odolnosti storage.py (bez discord.py)."""

import os
import tempfile
import unittest
from unittest import mock

import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_save_and_load_roundtrip(self):
        storage.save_data("x.json", {"k": [1, 2]})
        self.assertEqual(storage.load_data("x.json", []), {"k": [1, 2]})

    def test_atomic_write_leaves_no_tmp(self):
        storage.save_data("x.json", [1, 2, 3])
        leftovers = [f for f in os.listdir(self._tmp) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_load_missing_returns_default(self):
        self.assertEqual(storage.load_data("nope.json", 42), 42)
        self.assertEqual(storage.load_data("nope.json"), [])

    def test_load_corrupt_returns_default_and_logs(self):
        with open(os.path.join(self._tmp, "bad.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertLogs("dachshundtiers", level="ERROR"):
            self.assertEqual(storage.load_data("bad.json", []), [])

    def test_save_failure_raises_and_keeps_original(self):
        storage.save_data("x.json", {"v": 1})
        with mock.patch.object(storage.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.save_data("x.json", {"v": 2})
        # původní soubor zůstal nedotčený a žádný tmp nezůstal
        self.assertEqual(storage.load_data("x.json"), {"v": 1})
        self.assertEqual(sorted(os.listdir(self._tmp)), ["x.json"])

    def test_overwrite_works(self):
        storage.save_data("x.json", [1])
        storage.save_data("x.json", [2, 3])
        self.assertEqual(storage.load_data("x.json"), [2, 3])


if __name__ == "__main__":
    unittest.main()
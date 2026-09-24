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

    def test_strict_mode_raises_on_corrupt(self):
        with open(os.path.join(self._tmp, "bad.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(storage.DataCorruptionError):
            storage.load_data("bad.json", [], strict=True)
        # soubor se nikdy nepřepíše defaultními daty
        with open(os.path.join(self._tmp, "bad.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_strict_mode_missing_file_returns_default(self):
        # strict řeší POŠKOZENÝ soubor; chybějící soubor je v pořádku (default)
        self.assertEqual(storage.load_data("nope.json", [], strict=True), [])

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

    def test_database_url_is_built_from_individual_environment_values(self):
        env = {
            "DATABASE_URL": "",
            "DB_HOST": "db.example.test",
            "DB_PORT": "5433",
            "DB_NAME": "tiers data",
            "DB_USER": "bot@example",
            "DB_PASSWORD": "secret/@: value",
        }
        with mock.patch.dict(storage.os.environ, env, clear=True):
            self.assertEqual(
                storage._database_url_from_environment(),
                "postgresql://bot%40example:secret%2F%40%3A%20value@"
                "db.example.test:5433/tiers%20data",
            )

    def test_explicit_database_url_has_precedence(self):
        with mock.patch.dict(
            storage.os.environ,
            {"DATABASE_URL": "postgresql://explicit/database", "DB_HOST": "ignored"},
            clear=True,
        ):
            self.assertEqual(
                storage._database_url_from_environment(), "postgresql://explicit/database"
            )

    def test_database_status_reports_json_mode_without_db_connection(self):
        with mock.patch.object(storage, "DATABASE_URL", ""):
            status = storage.database_status()
        self.assertEqual(status["backend"], "json")
        self.assertTrue(status["ok"])
        self.assertIsNone(status["records"])


if __name__ == "__main__":
    unittest.main()

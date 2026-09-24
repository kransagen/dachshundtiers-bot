"""Testy sjednocení názvů kitů – utils.py (item 8).

- ``get_kits``: case-insensitive dedup ze ``kits.json`` (první výskyt vyhrává),
  ignoruje prázdné / ne-textové položky; bez souboru → DEFAULT_KITS,
- ``canonical_kit_name``: registrovaný kit libovolného case → display-case
  název z kits.json, nezaregistrovaný → vstup beze změny,
- ``migrate_mode_keys``: case-variantní klíč v modes/history se bezeztrátově
  přejmenuje na kanonický – nikdy nevzniknou dva klíče jednoho kitu.
"""

import tempfile
import unittest
from unittest import mock

import storage
from utils import DEFAULT_KITS, canonical_kit_name, get_kits, migrate_mode_keys


class GetKitsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_defaults_when_no_file(self):
        self.assertEqual(get_kits(), list(DEFAULT_KITS))

    def test_case_duplicates_merged_first_wins(self):
        storage.save_data("kits.json", ["MolePVP", "molepvp", "MolePvP"])
        self.assertEqual(get_kits(), ["MolePVP"])

    def test_invalid_entries_skipped(self):
        storage.save_data("kits.json", ["AnchorPvP", "", "  ", 42, None])
        self.assertEqual(get_kits(), ["AnchorPvP"])

    def test_display_case_preserved_for_distinct_kits(self):
        storage.save_data("kits.json", ["AnchorPvP", "MolePVP"])
        self.assertEqual(get_kits(), ["AnchorPvP", "MolePVP"])


class CanonicalKitNameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data("kits.json", ["MolePVP"])

    def test_registered_kit_any_case_maps_to_display_case(self):
        for raw in ("molepvp", "MOLEPVP", " MolePVP ", "MolePvP"):
            self.assertEqual(canonical_kit_name(raw), "MolePVP", raw)

    def test_unregistered_kit_unchanged(self):
        self.assertEqual(canonical_kit_name("randompot"), "randompot")

    def test_empty_input_unchanged(self):
        self.assertEqual(canonical_kit_name(""), "")
        self.assertEqual(canonical_kit_name(None), "")


class MigrateModeKeysTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data("kits.json", ["MolePVP"])

    def test_variant_key_renamed_canonical_preserving_value(self):
        mapping = {"molepvp": "LT3"}
        key = migrate_mode_keys(mapping, "molepvp")
        self.assertEqual(key, "MolePVP")
        self.assertEqual(mapping, {"MolePVP": "LT3"})

    def test_canonical_key_untouched(self):
        mapping = {"MolePVP": "HT3", "AnchorPvP": "LT5"}
        key = migrate_mode_keys(mapping, "molepvp")
        self.assertEqual(key, "MolePVP")
        self.assertEqual(mapping, {"MolePVP": "HT3", "AnchorPvP": "LT5"})

    def test_missing_key_returns_canonical_for_write(self):
        mapping = {"AnchorPvP": "LT5"}
        key = migrate_mode_keys(mapping, "MolePVP")
        self.assertEqual(key, "MolePVP")
        self.assertEqual(mapping, {"AnchorPvP": "LT5"})


if __name__ == "__main__":
    unittest.main()
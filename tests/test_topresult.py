"""Testy žebříčku top výsledků – services/topresult.py (bez discord.py).

Pokrývají Phase 6 požadavky na /topresult:
- čte se JEN z kanonické players.json (žádná druhá databáze),
- používá skutečnou tier hierarchii projektu (HT3_TIER_LADDER),
- řazení: nejlepší tier → počet kitů s ním → počet záznamů → username,
- varianty zápisu („LT3 EVAL“, malá písmena) se normalizují,
- tiery mimo žebříček (S/A, R-tiery) se neskórují (hráči v „excluded"),
- filtry tier/limit/page (paginace) a vyhledání hráče (@player).
"""

import unittest

from services import topresult


def _player(username, modes=None, history=None):
    return {
        "username": username,
        "modes": dict(modes or {}),
        "history": dict(history or {}),
    }


T = {"AnchorPvP": "HT3", "NetheriteSword": "LT2"}


class BuildRankingTests(unittest.TestCase):
    def test_empty_and_clean_input(self):
        r = topresult.build_ranking([])
        self.assertEqual(r["ranked"], [])
        self.assertEqual(r["excluded"], [])
        self.assertEqual(r["total_players"], 0)

    def test_best_tier_decides_order(self):
        players = [
            _player("low", {"IronAxe": "HT5", "AnchorPvP": "LT4"}),
            _player("high", {"AnchorPvP": "HT3", "NetheriteSword": "LT1"}),
            _player("mid", {"AnchorPvP": "HT4"}),
        ]
        r = topresult.build_ranking(players)
        names = [e["username"] for e in r["ranked"]]
        self.assertEqual(names, ["high", "mid", "low"])
        # ranky 1..n pořadí
        self.assertEqual([e["rank"] for e in r["ranked"]], [1, 2, 3])

    def test_tiebreak_more_kits_at_best_tier_first(self):
        players = [
            _player("onekit", {"AnchorPvP": "HT3", "IronAxe": "LT5"}),
            _player("twokits", {"AnchorPvP": "HT3", "NetheriteSword": "HT3"}),
        ]
        r = topresult.build_ranking(players)
        self.assertEqual(r["ranked"][0]["username"], "twokits")
        self.assertEqual(r["ranked"][0]["best_kits_count"], 2)
        self.assertEqual(r["ranked"][0]["best_tier"], "HT3")

    def test_tiebreak_total_records(self):
        players = [
            _player("fewer", {"AnchorPvP": "HT3"}),
            _player("more", {"AnchorPvP": "HT3", "IronAxe": "LT5", "GoldSMP": "LT5"}),
        ]
        r = topresult.build_ranking(players)
        self.assertEqual(r["ranked"][0]["username"], "more")

    def test_tiebreak_alphabetical_case_insensitive(self):
        players = [
            _player("beta", {"AnchorPvP": "HT3"}),
            _player("Alpha", {"AnchorPvP": "HT3"}),
        ]
        r = topresult.build_ranking(players)
        self.assertEqual(r["ranked"][0]["username"], "Alpha")

    def test_shared_rank_for_exact_ties(self):
        players = [
            _player("a", {"AnchorPvP": "HT3"}),
            _player("b", {"AnchorPvP": "HT3"}),
            _player("c", {"AnchorPvP": "HT4"}),  # HT4 je horší než HT3 → 3. místo
        ]
        r = topresult.build_ranking(players)
        self.assertEqual(r["ranked"][0]["rank"], 1)
        self.assertEqual(r["ranked"][1]["rank"], 1)
        self.assertEqual(r["ranked"][2]["rank"], 3)

    def test_lt3_eval_normalized_to_lt3e(self):
        players = [
            _player("eval", {"AnchorPvP": "LT3 EVAL"}),
            _player("plain", {"AnchorPvP": "LT3E"}),
            _player("lt3", {"AnchorPvP": "LT3"}),
        ]
        r = topresult.build_ranking(players)
        self.assertEqual(r["ranked"][0]["username"], "eval")
        self.assertEqual(r["ranked"][0]["best_tier"], "LT3E")
        self.assertEqual(r["ranked"][0]["best_tier_display"], "LT3 + eval")
        self.assertEqual(r["ranked"][1]["username"], "plain")
        # LT3 (bez evalu) je horší než LT3E
        self.assertEqual(r["ranked"][2]["username"], "lt3")

    def test_lowercase_and_whitespace_tiers_normalized(self):
        players = [
            _player("lower", {"AnchorPvP": " ht3 "}),
            _player("upper", {"AnchorPvP": "HT3"}),
        ]
        r = topresult.build_ranking(players)
        first, second = r["ranked"]
        self.assertEqual(first["best_tier"], second["best_tier"])
        self.assertEqual(first["best_tier"], "HT3")
        # skóre je stejné, takže rozhoduje abeceda (lower < upper)
        self.assertEqual(first["username"], "lower")
        self.assertEqual(second["username"], "upper")

    def test_non_ladder_tiers_go_to_excluded(self):
        players = [
            _player("tournament", {"AnchorPvP": "S", "IronAxe": "A"}),
            _player("rtier", {"AnchorPvP": "RLT2"}),
            _player("normal", {"AnchorPvP": "HT3"}),
        ]
        r = topresult.build_ranking(players)
        self.assertEqual([e["username"] for e in r["ranked"]], ["normal"])
        self.assertEqual(sorted(r["excluded"]), ["rtier", "tournament"])
        self.assertEqual(r["total_players"], 3)
        self.assertEqual(r["total_ranked"], 1)

    def test_rating_kits_include_all_tiers(self):
        players = [_player("mix", {"AnchorPvP": "HT3", "IronAxe": "S"})]
        r = topresult.build_ranking(players)
        entry = r["ranked"][0]
        self.assertEqual(entry["best_tier"], "HT3")
        self.assertEqual(entry["kits"]["IronAxe"], "S")  # i turnajový tier zůstává
        self.assertEqual(entry["total"], 1)  # do skóre jde jen ladder

    def test_corrupt_entries_skipped(self):
        players = [
            "garbage",
            {"username": ""},
            {"username": "ok", "modes": "not-dict"},  # platný hráč bez tierů → mimo
            _player("good", {"AnchorPvP": "HT3"}),
        ]
        r = topresult.build_ranking(players)
        self.assertEqual([e["username"] for e in r["ranked"]], ["good"])
        self.assertEqual(r["excluded"], ["ok"])
        self.assertEqual(r["total_players"], 2)

    def test_does_not_mutate_input(self):
        players = [_player("a", {"AnchorPvP": "LT3 EVAL"})]
        import copy

        before = copy.deepcopy(players)
        topresult.build_ranking(players)
        self.assertEqual(players, before)


class FilterAndPaginateTests(unittest.TestCase):
    def setUp(self):
        self.players = [
            _player("ht3a", {"AnchorPvP": "HT3", "IronAxe": "HT5"}),  # HT5 < HT3
            _player("ht3b", {"AnchorPvP": "HT3"}),
            _player("lt2", {"AnchorPvP": "LT2"}),
            _player("ht1", {"AnchorPvP": "HT1"}),
        ]
        self.ranking = topresult.build_ranking(self.players)

    def test_filter_by_tier_exact(self):
        filtered = topresult.filter_by_tier(self.ranking["ranked"], "HT3")
        self.assertEqual(sorted(e["username"] for e in filtered), ["ht3a", "ht3b"])

    def test_filter_accepts_normalized_input(self):
        filtered = topresult.filter_by_tier(self.ranking["ranked"], "ht3")
        self.assertEqual(len(filtered), 2)

    def test_filter_invalid_tier_raises(self):
        with self.assertRaises(ValueError):
            topresult.filter_by_tier(self.ranking["ranked"], "XYZ")
        with self.assertRaises(ValueError):
            topresult.filter_by_tier(self.ranking["ranked"], "")

    def test_paginate_basic(self):
        page = topresult.paginate(self.ranking["ranked"], limit=2, page=1)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["total_pages"], 2)
        self.assertEqual(page["page"], 1)

    def test_paginate_last_page_and_clamp(self):
        page = topresult.paginate(self.ranking["ranked"], limit=2, page=99)
        self.assertEqual(page["page"], 2)  # stránka nad rámec → poslední
        self.assertEqual(len(page["items"]), 2)

    def test_paginate_empty(self):
        page = topresult.paginate([], limit=10, page=3)
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["total_pages"], 1)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["page"], 1)

    def test_paginate_clamps_limit(self):
        page = topresult.paginate(self.ranking["ranked"], limit=0, page=1)
        self.assertEqual(page["limit"], 1)
        page = topresult.paginate(self.ranking["ranked"], limit=999, page=1)
        self.assertEqual(page["limit"], topresult.MAX_LIMIT)

    def test_paginate_default_limit(self):
        page = topresult.paginate(self.ranking["ranked"])
        self.assertEqual(page["limit"], topresult.DEFAULT_LIMIT)


class FindPlayerAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.players = [
            _player("AliceMC", {"AnchorPvP": "HT3"}),
            _player("Bob", {"AnchorPvP": "S"}),
        ]
        self.ranking = topresult.build_ranking(self.players)

    def test_find_player_case_insensitive(self):
        self.assertIsNotNone(topresult.find_player(self.players, ["aliceMC"]))
        self.assertIsNotNone(topresult.find_player(self.players, ["ALICEMC"]))
        self.assertIsNone(topresult.find_player(self.players, ["Nobody"]))
        self.assertIsNone(topresult.find_player(self.players, []))

    def test_find_player_matches_any_candidate(self):
        self.assertIsNotNone(topresult.find_player(self.players, ["x", "Bob", "y"]))
        self.assertIsNone(topresult.find_player(self.players, ["x", "y"]))

    def test_summary_ranked_player(self):
        summary = topresult.player_summary(self.ranking, self.players[0])
        self.assertEqual(summary["username"], "AliceMC")
        self.assertEqual(summary["rank"], 1)
        self.assertEqual(summary["best_tier"], "HT3")
        self.assertEqual(summary["total"], 1)
        self.assertFalse(summary["excluded"])
        self.assertEqual(
            summary["kits"], [("AnchorPvP", "HT3")]
        )  # (kit, displejový tier)

    def test_summary_excluded_player(self):
        summary = topresult.player_summary(self.ranking, self.players[1])
        self.assertEqual(summary["username"], "Bob")
        self.assertIsNone(summary["rank"])
        self.assertTrue(summary["excluded"])
        self.assertEqual(summary["kits"], [("AnchorPvP", "S")])

    def test_summary_exists_even_when_player_record_corrupt(self):
        self.assertIsNone(topresult.find_player(["x", None], ["nope"]))


class NormalizationTests(unittest.TestCase):
    def test_normalize_tier(self):
        self.assertEqual(topresult.normalize_tier(" ht3 "), "HT3")
        self.assertEqual(topresult.normalize_tier("LT3 EVAL"), "LT3E")
        self.assertEqual(topresult.normalize_tier("LT3 EVALUATION"), "LT3E")
        self.assertEqual(topresult.normalize_tier("RLT2"), "RLT2")
        self.assertEqual(topresult.normalize_tier(None), "")
        self.assertEqual(topresult.normalize_tier(42), "42")

    def test_tier_rank(self):
        self.assertEqual(topresult.tier_rank("lt5"), 0)
        self.assertEqual(topresult.tier_rank("HT1"), len(topresult.HT3_TIER_LADDER) - 1)
        self.assertEqual(topresult.tier_rank("LT3 EVAL"), topresult.tier_rank("LT3E"))
        self.assertIsNone(topresult.tier_rank("S"))
        self.assertIsNone(topresult.tier_rank("XYZ"))

    def test_validate_filter_tier(self):
        self.assertEqual(topresult.validate_filter_tier("ht2"), "HT2")
        self.assertEqual(topresult.validate_filter_tier("LT3 EVAL"), "LT3E")
        self.assertIsNone(topresult.validate_filter_tier("S"))
        self.assertIsNone(topresult.validate_filter_tier("foo"))

    def test_tier_display(self):
        self.assertEqual(topresult.tier_display("LT3E"), "LT3 + eval")
        self.assertEqual(topresult.tier_display("HT3"), "HT3")


if __name__ == "__main__":
    unittest.main()
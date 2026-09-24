"""Testy bezpečné synchronizace webu – services/checkweb.py (bez discord.py).

Pokrývají novou verzi /checkweb:
- porovnání tří zdrojů per (hráč × kit): Discord role × players.json × web,
- statusy: MATCH, MISSING_DISCORD_ROLE, DATABASE_MISMATCH, WEBSITE_MISMATCH,
  MULTIPLE_TIER_ROLES (KONFLIKT), UNKNOWN_ROLE, UNKNOWN_PLAYER,
  DUPLICATE_PLAYER,
- analýza NIKDY nic nemění (Discord role nejsou autoritativní zdroj),
- MULTIPLE_TIER_ROLES = KONFLIKT, nikdy se nevybírá automaticky,
- aplikace jen přes per-záznamová rozhodnutí (use_discord / keep_database /
  ignore) – „Use Discord" mění JEN modes, BEZ zápisu do historie,
- auditní log data/checkweb_log.json (actor, player, kit, old/new tier,
  reason, timestamp, source; append-only, restart-safe).
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import storage
from services import checkweb
from services.playersync import make_member


def _member(member_id, name, roles, extra=None):
    return make_member(member_id, name, roles, extra_names=extra)


def _player(username, modes=None):
    return {"username": username, "modes": dict(modes or {})}


def _analyze(players, members, roles_map, kit_display=None, website=None):
    return checkweb.analyze_checkweb(
        players=players,
        website=website,
        members=members,
        roles_map=roles_map,
        kit_display=kit_display or {"anchorpvp": "AnchorPvP", "sword": "Sword"},
    )


KIT = {"anchorpvp": "AnchorPvP", "sword": "Sword"}
ROLES_ANCHOR = {"anchorpvp": {"HT3": "102", "HT4": "103", "LT2": "104"}}


class AnalyzeCheckWebTests(unittest.TestCase):
    """Čistá analýza – žádný zápis do storage, žádné discord.py."""

    def test_match_when_all_three_agree(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        website = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT, website)
        self.assertEqual(analysis["summary"]["MATCH"], 1)
        self.assertEqual(analysis["checked"], 1)
        self.assertFalse(analysis["has_issues"])
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "MATCH")
        self.assertEqual(rec["db"], "HT3")
        self.assertEqual(rec["discord"], ["HT3"])
        self.assertEqual(rec["web"], "HT3")
        self.assertFalse(rec["resolvable"])

    def test_match_without_website(self):
        # web nelze přečíst (bez GITHUB_TOKEN) – porovnání jen role × DB
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT, website=None)
        self.assertEqual(analysis["summary"]["MATCH"], 1)
        self.assertFalse(analysis["records"][0].get("web"))

    def test_missing_discord_role(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        members = [_member("1", "AliceMC", set())]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["summary"]["MISSING_DISCORD_ROLE"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "MISSING_DISCORD_ROLE")
        self.assertEqual(rec["discord"], [])
        self.assertFalse(rec["resolvable"])  # je to projekce DB – opraví /playersync

    def test_database_mismatch_discord(self):
        # zadání: Discord HT4 vs DB HT3 vs web HT3
        players = [_player("Steve", {"Sword": "HT3"})]
        website = [_player("Steve", {"Sword": "HT3"})]
        members = [_member("1", "Steve", {"103"})]  # HT4
        analysis = _analyze(
            players, members, {"sword": {"HT3": "102", "HT4": "103"}}, KIT, website
        )
        self.assertEqual(analysis["summary"]["DATABASE_MISMATCH"], 1)
        rec = analysis["resolvable"][0]
        self.assertEqual(rec["db"], "HT3")
        self.assertEqual(rec["discord"], ["HT4"])
        self.assertEqual(rec["web"], "HT3")
        keys = [o["key"] for o in rec["options"]]
        self.assertIn("use_discord:HT4", keys)
        self.assertIn("keep_database", keys)
        self.assertIn("ignore", keys)

    def test_website_mismatch_info_only(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        website = [_player("AliceMC", {"AnchorPvP": "HT4"})]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT, website)
        self.assertEqual(analysis["summary"]["WEBSITE_MISMATCH"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "WEBSITE_MISMATCH")
        self.assertFalse(rec["resolvable"])  # srovná /websync, ne /checkweb

    def test_multiple_tier_roles_is_conflict_never_auto(self):
        # zadání: Steve drží HT3 + HT4 role, DB HT3 – CONFLICT
        players = [_player("Steve", {"Sword": "HT3"})]
        members = [_member("1", "Steve", {"102", "103"})]  # HT3 + HT4
        analysis = _analyze(
            players, members, {"sword": {"HT3": "102", "HT4": "103"}}, KIT
        )
        self.assertEqual(analysis["summary"]["MULTIPLE_TIER_ROLES"], 1)
        rec = analysis["resolvable"][0]
        self.assertEqual(rec["status"], "MULTIPLE_TIER_ROLES")
        self.assertIn("KONFLIKT", rec["label"])
        self.assertEqual(rec["discord"], ["HT3", "HT4"])
        keys = [o["key"] for o in rec["options"]]
        # obě role se nabízejí zvlášť – žádná se nevybírá sama
        self.assertIn("use_discord:HT3", keys)
        self.assertIn("use_discord:HT4", keys)
        self.assertIn("keep_database", keys)
        self.assertIn("ignore", keys)

    def test_db_missing_tier_discord_has(self):
        players = [_player("Bob", {})]
        members = [_member("1", "Bob", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["summary"]["DATABASE_MISMATCH"], 1)
        rec = analysis["resolvable"][0]
        self.assertIsNone(rec["db"])
        self.assertEqual(rec["discord"], ["HT3"])
        self.assertIn("use_discord:HT3", [o["key"] for o in rec["options"]])

    def test_unknown_role_unregistered_kit(self):
        # role namapovaná na kit, který není v kits.json (kit_display)
        members = [_member("1", "X", {"999"})]
        analysis = _analyze(
            [], members, {"ghostkit": {"HT3": "999"}}, KIT
        )
        self.assertEqual(analysis["summary"]["UNKNOWN_ROLE"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "UNKNOWN_ROLE")
        self.assertFalse(rec["resolvable"])

    def test_unknown_player_report_only(self):
        # člen drží tier roli, ale v players.json žádný takový hráč není
        members = [_member("5", "Zombie", {"102"})]
        analysis = _analyze([], members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["summary"]["UNKNOWN_PLAYER"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "UNKNOWN_PLAYER")
        self.assertIsNone(rec["db"])
        self.assertFalse(rec["resolvable"])  # zdroj pravdy je /result, ne Discord role

    def test_duplicate_player_database(self):
        players = [
            _player("AliceMC", {"AnchorPvP": "HT3"}),
            _player("aliceMC", {"AnchorPvP": "LT2"}),
        ]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["summary"]["DUPLICATE_PLAYER"], 1)
        self.assertEqual(analysis["summary"]["MATCH"], 0)  # řeší se až po sloučení
        self.assertEqual(
            analysis["records"][0]["status"], "DUPLICATE_PLAYER"
        )

    def test_duplicate_player_website(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        website = [
            _player("AliceMC", {"AnchorPvP": "HT3"}),
            _player("AliceMC", {"AnchorPvP": "LT2"}),
        ]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT, website)
        self.assertEqual(analysis["summary"]["DUPLICATE_PLAYER"], 1)
        dup = next(r for r in analysis["records"] if r["status"] == "DUPLICATE_PLAYER")
        self.assertEqual(dup["scope"], "website")

    def test_case_insensitive_matching(self):
        players = [_player("aliceMC", {"molepvp": "HT3"})]
        members = [_member("1", "ALICEMC", {"102"})]
        analysis = _analyze(
            players, members, {"molepvp": {"HT3": "102"}}, {"molepvp": "MolePVP"}
        )
        self.assertEqual(analysis["summary"]["MATCH"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["kit"], "MolePVP")

    def test_lt3_eval_normalized(self):
        players = [_player("AliceMC", {"AnchorPvP": "LT3 EVAL"})]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(
            players, members, {"anchorpvp": {"LT3E": "102"}}, KIT
        )
        self.assertEqual(analysis["summary"]["MATCH"], 1)
        self.assertEqual(analysis["records"][0]["db"], "LT3E")

    def test_analysis_is_pure(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        members = [_member("1", "AliceMC", {"103"})]  # HT4
        before = {"players": [dict(p) for p in players], "members": [dict(m) for m in members]}
        _analyze(players, members, ROLES_ANCHOR, KIT)
        # vstupy se NIKDY nemění (bez automatických zápisů)
        self.assertEqual(players, before["players"])
        self.assertEqual([dict(m) for m in members], before["members"])


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_stable_and_sensitive(self):
        roles = {"sword": {"HT3": "202", "HT4": "203"}}

        def run(players, members):
            return checkweb.analyze_checkweb(
                players=players, members=members, roles_map=roles, kit_display=KIT
            )

        a = run([_player("Steve", {"Sword": "HT3"})], [_member("1", "Steve", {"203"})])
        b = run([_player("Steve", {"Sword": "HT3"})], [_member("1", "Steve", {"203"})])
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        c = run([_player("Steve", {"Sword": "HT4"})], [_member("1", "Steve", {"203"})])
        self.assertNotEqual(a["fingerprint"], c["fingerprint"])

    def test_fingerprint_empty_when_nothing_resolvable(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["fingerprint"], "")


class ApplyCheckWebTests(unittest.TestCase):
    """Aplikace potvrzených rozhodnutí – jen modes, bez historie."""

    def _players(self):
        return [
            {"username": "Steve", "modes": {"Sword": "HT3"},
             "history": {"Sword": [{"date": "2024-01-01", "tier": "HT3"}]}},
            {"username": "AliceMC", "modes": {"AnchorPvP": "LT2"}},
        ]

    def _analysis(self):
        players = [{"username": "Steve", "modes": {"Sword": "HT3"}}]
        members = [_member("1", "Steve", {"103"})]  # HT4
        return _analyze(
            players, members, {"sword": {"HT3": "102", "HT4": "103"}}, KIT
        )

    def test_use_discord_updates_modes_but_not_history(self):
        players = self._players()
        analysis = self._analysis()
        decisions = [
            {"player": "Steve", "kit_key": "sword", "decision": "use_discord", "tier": "HT4"}
        ]
        new_players, applied = checkweb.apply_checkweb_decisions(
            players=players, records=analysis["records"],
            decisions=decisions, kit_display=KIT,
        )
        steve = next(p for p in new_players if p["username"] == "Steve")
        self.assertEqual(steve["modes"]["Sword"], "HT4")
        # historie se NIKDY nemění (žádný auto-zápis)
        self.assertEqual(steve["history"]["Sword"], [{"date": "2024-01-01", "tier": "HT3"}])
        a = applied[0]
        self.assertTrue(a["ok"])
        self.assertEqual(a["player"], "Steve")
        self.assertEqual(a["kit"], "Sword")
        self.assertEqual(a["oldTier"], "HT3")
        self.assertEqual(a["newTier"], "HT4")
        self.assertEqual(a["reason"], "use_discord")
        self.assertEqual(a["source"], "discord")

    def test_use_discord_when_no_mode_yet(self):
        players = self._players()
        analysis = self._analysis()
        # Steve nemá v DB "sword" – hmm, má. Použijeme Alici na nový kit.
        decisions = [
            {"player": "AliceMC", "kit_key": "anchorpvp", "decision": "use_discord", "tier": "HT3"}
        ]
        players = [{"username": "AliceMC", "modes": {"Sword": "LT2"}}]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT)
        new_players, applied = checkweb.apply_checkweb_decisions(
            players=players, records=analysis["records"],
            decisions=decisions, kit_display=KIT,
        )
        alice = new_players[0]
        self.assertEqual(alice["modes"]["AnchorPvP"], "HT3")
        self.assertEqual(alice["modes"]["Sword"], "LT2")  # ostatní módy zůstávají
        self.assertTrue(applied[0]["ok"])

    def test_existing_mode_key_case_preserved(self):
        players = [{"username": "AliceMC", "modes": {"molepvp": "LT2"}}]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(
            players, members, {"molepvp": {"HT3": "102"}}, {"molepvp": "MolePVP"}
        )
        decisions = [
            {"player": "AliceMC", "kit_key": "molepvp", "decision": "use_discord", "tier": "HT3"}
        ]
        new_players, _ = checkweb.apply_checkweb_decisions(
            players=players, records=analysis["records"],
            decisions=decisions, kit_display={"molepvp": "MolePVP"},
        )
        self.assertEqual(new_players[0]["modes"], {"molepvp": "HT3"})

    def test_keep_database_and_ignore_no_change(self):
        players = self._players()
        analysis = self._analysis()
        decisions = [
            {"player": "Steve", "kit_key": "sword", "decision": "keep_database"},
            {"player": "Steve", "kit_key": "sword", "decision": "ignore"},
        ]
        new_players, applied = checkweb.apply_checkweb_decisions(
            players=players, records=analysis["records"],
            decisions=decisions, kit_display=KIT,
        )
        steve = next(p for p in new_players if p["username"] == "Steve")
        self.assertEqual(steve["modes"]["Sword"], "HT3")
        self.assertTrue(all(a["ok"] for a in applied))
        self.assertEqual(applied[0]["newTier"], applied[0]["oldTier"])
        self.assertEqual(applied[0]["reason"], "keep_database")
        self.assertEqual(applied[0]["source"], "database")

    def test_invalid_tier_decision_fails(self):
        analysis = self._analysis()
        decisions = [
            {"player": "Steve", "kit_key": "sword", "decision": "use_discord", "tier": "HT1"}
        ]
        _, applied = checkweb.apply_checkweb_decisions(
            players=self._players(), records=analysis["records"],
            decisions=decisions, kit_display=KIT,
        )
        self.assertFalse(applied[0]["ok"])
        self.assertEqual(applied[0]["error"], "neplatný Discord tier")

    def test_record_missing_fails(self):
        players = self._players()
        analysis = self._analysis()
        decisions = [
            {"player": "Nope", "kit_key": "sword", "decision": "use_discord", "tier": "HT4"}
        ]
        _, applied = checkweb.apply_checkweb_decisions(
            players=players, records=analysis["records"],
            decisions=decisions, kit_display=KIT,
        )
        self.assertFalse(applied[0]["ok"])
        self.assertIn("neexistuje", applied[0]["error"])

    def test_unknown_decision_fails(self):
        analysis = self._analysis()
        decisions = [
            {"player": "Steve", "kit_key": "sword", "decision": "delete_db", "tier": None}
        ]
        _, applied = checkweb.apply_checkweb_decisions(
            players=self._players(), records=analysis["records"],
            decisions=decisions, kit_display=KIT,
        )
        self.assertFalse(applied[0]["ok"])


class DiscordImportDecisionTests(unittest.TestCase):
    def test_imports_only_single_tier_database_mismatches(self):
        records = [
            {
                "status": "DATABASE_MISMATCH",
                "player": "AliceMC",
                "kit_key": "anchorpvp",
                "discord": ["HT3"],
            },
            {
                "status": "MULTIPLE_TIER_ROLES",
                "player": "Bob",
                "kit_key": "sword",
                "discord": ["HT3", "HT4"],
            },
            {
                "status": "UNKNOWN_PLAYER",
                "player": "DiscordName",
                "kit_key": "sword",
                "discord": ["HT3"],
            },
            {
                "status": "MATCH",
                "player": "Carol",
                "kit_key": "sword",
                "discord": ["HT3"],
            },
        ]
        decisions, skipped = checkweb.build_discord_import_decisions(records)
        self.assertEqual(
            decisions,
            [
                {
                    "player": "AliceMC",
                    "kit_key": "anchorpvp",
                    "decision": "use_discord",
                    "tier": "HT3",
                }
            ],
        )
        self.assertEqual([r["player"] for r in skipped], ["Bob", "DiscordName"])


class CheckWebAuditLogTests(unittest.TestCase):
    """Auditní log data/checkweb_log.json (append-only, restart-safe)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data(checkweb.CHECKWEB_LOG_FILE, [])

    def test_log_contains_per_decision_audit_fields(self):
        async def main():
            repairs = [
                {"player": "Steve", "kit": "Sword", "kit_key": "sword",
                 "oldTier": "HT3", "newTier": "HT4", "reason": "use_discord",
                 "source": "discord", "ok": True, "error": None},
                {"player": "AliceMC", "kit": "AnchorPvP", "kit_key": "anchorpvp",
                 "oldTier": "LT2", "newTier": "LT2", "reason": "ignore",
                 "source": "database", "ok": True, "error": None},
            ]
            return await checkweb.log_checkweb_event(
                actor_id="42", actor_name="boss", mode="apply",
                status="success",
                summary={"DATABASE_MISMATCH": 2},
                website="GitHub",
                repairs=repairs,
                ts=99,
            )

        entry = asyncio.run(main())
        self.assertEqual(entry["ts"], 99)
        self.assertEqual(entry["mode"], "apply")
        self.assertEqual(entry["actorId"], "42")
        self.assertEqual(entry["actorName"], "boss")
        self.assertEqual(len(entry["repairs"]), 2)
        for r in entry["repairs"]:
            # povinná auditní pole: actor, player, kit, old/new tier, reason,
            # timestamp, source
            for key in ("actor", "actorId", "player", "kit", "oldTier",
                        "newTier", "reason", "timestamp", "source"):
                self.assertIn(key, r)
            self.assertEqual(r["actor"], "boss")
            self.assertEqual(r["actorId"], "42")
            self.assertEqual(r["timestamp"], 99)
        self.assertEqual(entry["repairs"][0]["player"], "Steve")
        self.assertEqual(entry["repairs"][0]["oldTier"], "HT3")
        self.assertEqual(entry["repairs"][0]["newTier"], "HT4")
        self.assertEqual(entry["repairs"][0]["source"], "discord")

    def test_log_append_only_restart_safe(self):
        async def main():
            await checkweb.log_checkweb_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", ts=1,
            )
            await checkweb.log_checkweb_event(
                actor_id="1", actor_name="admin", mode="apply",
                status="success", repairs=[], ts=2,
            )

        asyncio.run(main())
        asyncio.run(main())  # nový event loop = simulace restartu
        entries = storage.load_data(checkweb.CHECKWEB_LOG_FILE, [])
        self.assertEqual([e["ts"] for e in entries], [1, 2, 1, 2])
        self.assertEqual([e["mode"] for e in entries], ["preview", "apply"] * 2)

    def test_corrupted_log_is_reset_instead_of_crash(self):
        storage.save_data(checkweb.CHECKWEB_LOG_FILE, "not-a-list")

        async def main():
            await checkweb.log_checkweb_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", ts=5,
            )

        asyncio.run(main())
        entries = storage.load_data(checkweb.CHECKWEB_LOG_FILE, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], 5)

    def test_get_log_skips_non_dict_entries(self):
        async def main():
            await checkweb.log_checkweb_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", ts=1,
            )
            return await checkweb.get_checkweb_log()

        entries = asyncio.run(main())
        self.assertEqual(len(entries), 1)
        self.assertIn("ts", entries[0])


if __name__ == "__main__":
    unittest.main()

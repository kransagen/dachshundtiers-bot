"""Testy diagnostiky Discord × PostgreSQL × web – services/checkweb.py (bez discord.py).

Pokrývají podklad /sync check:
- porovnání tří zdrojů per (hráč × kit): Discord role × DB × web,
- statusy: MATCH, MISSING_DISCORD_ROLE, DATABASE_MISMATCH, WEBSITE_MISMATCH,
  MULTIPLE_TIER_ROLES (KONFLIKT), UNKNOWN_ROLE, UNKNOWN_PLAYER,
  DUPLICATE_PLAYER,
- analýza NIKDY nic nemění,
- MULTIPLE_TIER_ROLES = KONFLIKT, nikdy se nevybírá automaticky,
- auditní log data/checkweb_log.json (append-only, restart-safe, rotace).
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

    def test_database_mismatch_discord(self):
        # zadání: Discord HT4 vs DB HT3 vs web HT3
        players = [_player("Steve", {"Sword": "HT3"})]
        website = [_player("Steve", {"Sword": "HT3"})]
        members = [_member("1", "Steve", {"103"})]  # HT4
        analysis = _analyze(
            players, members, {"sword": {"HT3": "102", "HT4": "103"}}, KIT, website
        )
        self.assertEqual(analysis["summary"]["DATABASE_MISMATCH"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "DATABASE_MISMATCH")
        self.assertEqual(rec["db"], "HT3")
        self.assertEqual(rec["discord"], ["HT4"])
        self.assertEqual(rec["web"], "HT3")

    def test_website_mismatch_info_only(self):
        players = [_player("AliceMC", {"AnchorPvP": "HT3"})]
        website = [_player("AliceMC", {"AnchorPvP": "HT4"})]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT, website)
        self.assertEqual(analysis["summary"]["WEBSITE_MISMATCH"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "WEBSITE_MISMATCH")

    def test_multiple_tier_roles_is_conflict_never_auto(self):
        # zadání: Steve drží HT3 + HT4 role, DB HT3 – CONFLICT
        players = [_player("Steve", {"Sword": "HT3"})]
        members = [_member("1", "Steve", {"102", "103"})]  # HT3 + HT4
        analysis = _analyze(
            players, members, {"sword": {"HT3": "102", "HT4": "103"}}, KIT
        )
        self.assertEqual(analysis["summary"]["MULTIPLE_TIER_ROLES"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "MULTIPLE_TIER_ROLES")
        self.assertIn("KONFLIKT", rec["label"])
        # obě role zůstávají vidět – žádná se nevybírá sama
        self.assertEqual(rec["discord"], ["HT3", "HT4"])

    def test_db_missing_tier_discord_has(self):
        players = [_player("Bob", {})]
        members = [_member("1", "Bob", {"102"})]
        analysis = _analyze(players, members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["summary"]["DATABASE_MISMATCH"], 1)
        rec = analysis["records"][0]
        self.assertIsNone(rec["db"])
        self.assertEqual(rec["discord"], ["HT3"])

    def test_unknown_role_unregistered_kit(self):
        # role namapovaná na kit, který není v kits.json (kit_display)
        members = [_member("1", "X", {"999"})]
        analysis = _analyze(
            [], members, {"ghostkit": {"HT3": "999"}}, KIT
        )
        self.assertEqual(analysis["summary"]["UNKNOWN_ROLE"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "UNKNOWN_ROLE")

    def test_unknown_player_report_only(self):
        # člen drží tier roli, ale v players.json žádný takový hráč není
        members = [_member("5", "Zombie", {"102"})]
        analysis = _analyze([], members, ROLES_ANCHOR, KIT)
        self.assertEqual(analysis["summary"]["UNKNOWN_PLAYER"], 1)
        rec = analysis["records"][0]
        self.assertEqual(rec["status"], "UNKNOWN_PLAYER")
        self.assertIsNone(rec["db"])

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


class CheckWebAuditLogTests(unittest.TestCase):
    """Auditní log data/checkweb_log.json (append-only, restart-safe)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data(checkweb.CHECKWEB_LOG_FILE, [])

    def test_log_entry_has_audit_fields(self):
        async def main():
            return await checkweb.log_checkweb_event(
                actor_id="42", actor_name="boss", mode="preview",
                status="success",
                summary={"DATABASE_MISMATCH": 2},
                website="GitHub",
                errors=["rate limited"],
                ts=99,
            )

        entry = asyncio.run(main())
        self.assertEqual(entry["ts"], 99)
        self.assertEqual(entry["mode"], "preview")
        self.assertEqual(entry["actorId"], "42")
        self.assertEqual(entry["actorName"], "boss")
        self.assertEqual(entry["errors"], ["rate limited"])
        self.assertEqual(entry["summary"], {"DATABASE_MISMATCH": 2})

    def test_log_keeps_only_last_limit_entries(self):
        async def main():
            with mock.patch.object(checkweb, "AUDIT_LOG_LIMIT", 3):
                for ts in range(1, 6):
                    await checkweb.log_checkweb_event(
                        actor_id="1", actor_name="admin", mode="preview", ts=ts
                    )

        asyncio.run(main())
        entries = storage.load_data(checkweb.CHECKWEB_LOG_FILE, [])
        self.assertEqual([e["ts"] for e in entries], [3, 4, 5])

    def test_log_append_only_restart_safe(self):
        async def main():
            await checkweb.log_checkweb_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", ts=1,
            )
            await checkweb.log_checkweb_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", ts=2,
            )

        asyncio.run(main())
        asyncio.run(main())  # nový event loop = simulace restartu
        entries = storage.load_data(checkweb.CHECKWEB_LOG_FILE, [])
        self.assertEqual([e["ts"] for e in entries], [1, 2, 1, 2])
        self.assertEqual([e["mode"] for e in entries], ["preview"] * 4)

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

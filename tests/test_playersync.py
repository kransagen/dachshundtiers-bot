"""Testy synchronizace tier rolí – services/playersync.py (bez discord.py).

Pokrývají Phase 4 požadavky:
- detekce chybějících rolí, špatných rolí, více tier rolí, neznámých hráčů,
  chybějících hráčů a neplatných tierů,
- analýza NIKDY nic nemění (jen navrhuje akce),
- žádné automatické řešení konfliktů – změny se aplikují teprve po potvrzení
  (otisk akcí porovnává náhled s potvrzením),
- deduplikace akcí,
- auditní log data/playersync_log.json (append-only, restart-safe).
"""

import asyncio
import copy
import tempfile
import unittest
from unittest import mock

import storage
from services import playersync


def _member(member_id, name, roles, extra=None):
    return playersync.make_member(member_id, name, roles, extra_names=extra)


def _analyze(players, members, roles, kit_display=None):
    return playersync.analyze_sync(
        players, members, roles, kit_display or {}
    )


class AnalyzeSyncTests(unittest.TestCase):
    """Čistá analýza – žádný zápis do storage, žádné discord.py."""

    def test_missing_role_detected_with_add_action(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "AliceMC", {"909"})]  # hráč, ale bez role HT3
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertTrue(analysis["has_actions"])
        self.assertEqual(analysis["summary"]["missing_role"], 1)
        self.assertEqual(analysis["summary"]["wrong_role"], 0)
        f = analysis["findings"][0]
        self.assertEqual(f["kind"], "missing_role")
        self.assertEqual(f["member_id"], "1")
        self.assertEqual(f["ign"], "AliceMC")
        self.assertEqual(f["expected_tier"], "HT3")
        self.assertEqual(
            f["action"],
            {
                "op": "add",
                "member_id": "1",
                "member_name": "AliceMC",
                "role_id": "102",
                "kit": "AnchorPvP",
                "kit_key": "anchorpvp",
                "tier": "HT3",
            },
        )

    def test_no_findings_when_roles_match(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertFalse(analysis["findings"])
        self.assertFalse(analysis["has_actions"])
        self.assertFalse(analysis["has_issues"])
        self.assertEqual(analysis["checked"], 1)

    def test_wrong_role_and_missing_role(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "AliceMC", {"111"})]  # HT5, ne HT3
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT5": "111", "HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["wrong_role"], 1)
        self.assertEqual(analysis["summary"]["missing_role"], 1)
        kinds = {f["kind"] for f in analysis["findings"]}
        self.assertEqual(kinds, {"wrong_role", "missing_role"})
        # přidat správnou roli + odebrat špatnou
        self.assertEqual(
            [a["op"] for a in analysis["actions"]], ["add", "remove"]
        )
        self.assertEqual(analysis["actions"][0]["role_id"], "102")
        self.assertEqual(analysis["actions"][1]["role_id"], "111")

    def test_multiple_roles_detected_keeps_expected(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "AliceMC", {"102", "103"})]  # HT3 + LT2
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT3": "102", "LT2": "103"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["multiple_roles"], 1)
        self.assertEqual(analysis["summary"]["wrong_role"], 1)
        self.assertEqual(analysis["summary"]["missing_role"], 0)
        # správná role (102) zůstává, LT2 (103) se odebírá
        self.assertEqual([a["role_id"] for a in analysis["actions"]], ["103"])
        multiple = next(
            f for f in analysis["findings"] if f["kind"] == "multiple_roles"
        )
        self.assertEqual(multiple["actual_tier"], "HT3, LT2")

    def test_multiple_roles_without_expected_all_removed(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        # drží LT2 + HT5, ale roli HT3 (102) nemá
        members = [_member("1", "AliceMC", {"103", "111"})]
        analysis = _analyze(
            players,
            members,
            {"anchorpvp": {"HT5": "111", "LT2": "103", "HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["multiple_roles"], 1)
        self.assertEqual(analysis["summary"]["missing_role"], 1)
        self.assertEqual(analysis["summary"]["wrong_role"], 2)
        removed = sorted(
            a["role_id"] for a in analysis["actions"] if a["op"] == "remove"
        )
        self.assertEqual(removed, ["103", "111"])
        added = [a["role_id"] for a in analysis["actions"] if a["op"] == "add"]
        self.assertEqual(added, ["102"])

    def test_unknown_player_role_removed(self):
        members = [_member("5", "Zombie", {"102"})]
        analysis = _analyze(
            [], members, {"anchorpvp": {"LT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["unknown_player"], 1)
        self.assertEqual(analysis["actions"][0]["op"], "remove")
        self.assertEqual(analysis["actions"][0]["role_id"], "102")
        f = analysis["findings"][0]
        self.assertEqual(f["kind"], "unknown_player")
        self.assertTrue("Zombie" in f["message"])

    def test_multiple_roles_for_unknown_player(self):
        members = [_member("5", "Zombie", {"102", "103"})]
        analysis = _analyze(
            [], members, {"anchorpvp": {"LT3": "102", "LT2": "103"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["multiple_roles"], 1)
        self.assertEqual(analysis["summary"]["unknown_player"], 2)
        self.assertEqual(len(analysis["actions"]), 2)

    def test_player_without_tier_for_kit_wrong_role(self):
        # hráč v DB je, ale pro tento kit v players.json tier nemá
        players = [{"username": "AliceMC", "modes": {"OtherKit": "LT3"}}]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(
            players, members, {"anchorpvp": {"LT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["wrong_role"], 1)
        self.assertIsNone(analysis["findings"][0]["expected_tier"])
        self.assertEqual(analysis["actions"][0]["op"], "remove")

    def test_missing_player_report_only(self):
        players = [{"username": "GhostPlayer", "modes": {"AnchorPvP": "HT3"}}]
        analysis = _analyze(
            players, [], {"anchorpvp": {"HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["missing_player"], 1)
        f = analysis["findings"][0]
        self.assertEqual(f["kind"], "missing_player")
        self.assertIsNone(f["action"])
        self.assertFalse(analysis["has_actions"])

    def test_invalid_tier_report_only(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT9"}}]
        members = [_member("1", "AliceMC", {})]
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(analysis["summary"]["invalid_tier"], 1)
        f = analysis["findings"][0]
        self.assertEqual(f["kind"], "invalid_tier")
        self.assertIsNone(f["action"])
        self.assertEqual(f["expected_tier"], "HT9")
        self.assertFalse(analysis["has_actions"])

    def test_lowercase_db_tier_is_normalized(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "ht3"}}]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertFalse(analysis["findings"])

    def test_matching_is_case_insensitive_and_uses_extra_names(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "alice", {"102"}, extra=["AliceMC"])]
        analysis = _analyze(
            players, members, {"anchorpvp": {"HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertFalse(analysis["findings"])

    def test_kits_are_independent(self):
        roles = {"anchorpvp": {"HT3": "102"}, "ironaxe": {"LT3": "202"}}
        players = [
            {
                "username": "AliceMC",
                "modes": {"AnchorPvP": "HT3", "IronAxe": "LT3"},
            }
        ]
        members = [_member("1", "AliceMC", {"202"})]  # ironaxe OK, anchorpvp chybí
        analysis = _analyze(
            players, members, roles,
            {"anchorpvp": "AnchorPvP", "ironaxe": "IronAxe"},
        )
        self.assertEqual(analysis["summary"]["missing_role"], 1)
        self.assertEqual(analysis["summary"]["wrong_role"], 0)
        self.assertEqual(analysis["actions"][0]["role_id"], "102")

    def test_invalid_roles_map_entries_skipped(self):
        # role_id není číslo → mapování přeskočeno, žádný nález
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "AliceMC", {"102"})]
        analysis = _analyze(
            players, members,
            {"anchorpvp": {"HT3": "not-a-role-id"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertFalse(analysis["findings"])

    def test_empty_or_absent_mappings(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        analysis = _analyze(players, [_member("1", "AliceMC", {})], {})
        self.assertFalse(analysis["findings"])
        analysis = _analyze(players, [_member("1", "AliceMC", {})], None)
        self.assertFalse(analysis["findings"])

    def test_analysis_never_mutates_inputs(self):
        players = [
            {
                "username": "AliceMC",
                "modes": {"AnchorPvP": "HT3"},
                "history": {"AnchorPvP": []},
            }
        ]
        members = [_member("1", "AliceMC", {"111"})]
        players_before = copy.deepcopy(players)
        members_before = copy.deepcopy(members)
        _analyze(
            players, members, {"anchorpvp": {"HT5": "111", "HT3": "102"}},
            {"anchorpvp": "AnchorPvP"},
        )
        self.assertEqual(players, players_before)
        self.assertEqual(members, members_before)

    def test_build_actions_dedup_and_fingerprint(self):
        findings = [
            {
                "action": {
                    "op": "remove", "member_id": "1", "role_id": "101",
                    "tier": "LT5", "kit": "AnchorPvP", "kit_key": "anchorpvp",
                    "member_name": "a",
                }
            },
            {
                "action": {
                    "op": "remove", "member_id": "1", "role_id": "101",
                    "tier": "LT5", "kit": "AnchorPvP", "kit_key": "anchorpvp",
                    "member_name": "a",
                }
            },
            {"action": None},
            {
                "action": {
                    "op": "add", "member_id": "2", "role_id": "102",
                    "tier": "HT3", "kit": "AnchorPvP", "kit_key": "anchorpvp",
                    "member_name": "b",
                }
            },
        ]
        actions = playersync.build_actions(findings)
        self.assertEqual(len(actions), 2)

        fp1 = playersync.fingerprint(actions)
        fp2 = playersync.fingerprint(list(reversed(actions)))
        self.assertEqual(fp1, fp2)  # pořadí akcí nerozhoduje
        self.assertNotEqual(fp1, playersync.fingerprint(actions[:1]))

    def test_fingerprint_distinguishes_op_and_role(self):
        a = [
            {"op": "add", "member_id": "1", "role_id": "102"},
            {"op": "remove", "member_id": "1", "role_id": "103"},
        ]
        b = [
            {"op": "add", "member_id": "1", "role_id": "103"},
            {"op": "remove", "member_id": "1", "role_id": "102"},
        ]
        self.assertNotEqual(playersync.fingerprint(a), playersync.fingerprint(b))

    def test_findings_are_deterministic(self):
        players = [
            {"username": "Bob", "modes": {"AnchorPvP": "HT3"}},
            {"username": "Alice", "modes": {"AnchorPvP": "LT3"}},
            {"username": "Carol", "modes": {"AnchorPvP": "HT9"}},
        ]
        members = [
            _member("3", "Carol", {}),
            _member("1", "Alice", {"909"}),
            _member("2", "Bob", {"102", "103"}),
        ]
        roles = {"anchorpvp": {"HT3": "102", "LT2": "103", "LT3": "104"}}
        first = _analyze(players, members, roles)
        for _ in range(5):
            second = _analyze(list(reversed(players)), list(reversed(members)), roles)
            self.assertEqual(
                [f["message"] for f in first["findings"]],
                [f["message"] for f in second["findings"]],
            )


class PlayersyncAuditLogTests(unittest.TestCase):
    """Auditní log data/playersync_log.json (append-only, restart-safe)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data(playersync.PLAYERSYNC_LOG_FILE, [])

    def test_preview_and_apply_are_logged(self):
        async def main():
            preview_entry = await playersync.log_playersync_event(
                actor_id="1",
                actor_name="admin",
                mode="preview",
                summary={"missing_role": 2, "wrong_role": 1},
                actions=[{"op": "add", "member_id": "1", "role_id": "102"}],
                ts=1000,
            )
            apply_entry = await playersync.log_playersync_event(
                actor_id="1",
                actor_name="admin",
                mode="apply",
                summary={"missing_role": 2, "wrong_role": 1},
                applied=[
                    {
                        "op": "add", "memberId": "1", "roleId": "102",
                        "kit": "AnchorPvP", "tier": "HT3",
                        "ok": True, "error": None,
                    }
                ],
                ts=2000,
            )
            # vrací záznam
            self.assertEqual(preview_entry["mode"], "preview")
            self.assertEqual(apply_entry["mode"], "apply")
            # a uložil ho i do souboru
            entries = storage.load_data(playersync.PLAYERSYNC_LOG_FILE, [])
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["ts"], 1000)
            self.assertEqual(entries[0]["mode"], "preview")
            self.assertEqual(entries[0]["summary"]["missing_role"], 2)
            self.assertIn("plannedActions", entries[0])
            self.assertEqual(entries[1]["mode"], "apply")
            self.assertEqual(entries[1]["applied"][0]["ok"], True)
            # čtečka
            self.assertEqual(len(await playersync.get_playersync_log()), 2)

        asyncio.run(main())

    def test_log_survives_restart_across_loops(self):
        async def main():
            await playersync.log_playersync_event(
                actor_id="1", actor_name="x", mode="apply",
                summary={}, applied=[], ts=1,
            )

        asyncio.run(main())
        asyncio.run(main())  # nový event loop = simulace restartu
        entries = storage.load_data(playersync.PLAYERSYNC_LOG_FILE, [])
        self.assertEqual(len(entries), 2)

    def test_log_entries_are_append_only(self):
        async def main():
            await playersync.log_playersync_event(
                actor_id="1", actor_name="admin", mode="preview",
                summary={}, actions=[], ts=1,
            )
            for i in range(3):
                await playersync.log_playersync_event(
                    actor_id="1", actor_name="admin", mode="apply",
                    summary={"x": i}, applied=[], ts=10 + i,
                )

        asyncio.run(main())
        entries = storage.load_data(playersync.PLAYERSYNC_LOG_FILE, [])
        self.assertEqual([e["ts"] for e in entries], [1, 10, 11, 12])

    def test_corrupted_log_is_reset_instead_of_crash(self):
        storage.save_data(playersync.PLAYERSYNC_LOG_FILE, "not-a-list")

        async def main():
            await playersync.log_playersync_event(
                actor_id="1", actor_name="admin", mode="apply",
                summary={}, applied=[], ts=5,
            )

        asyncio.run(main())
        entries = storage.load_data(playersync.PLAYERSYNC_LOG_FILE, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], 5)


if __name__ == "__main__":
    unittest.main()
"""Testy kontroly integrity dat – services/datacheck.py (bez discord.py).

Pokrývají Phase 6 požadavky na /datacheck:
- kontroly: duplicitní hráči, duplicitní Discord ID, duplicitní IGN, neplatné
  tiery, konfliktní Discord role, chybějící webové záznamy, neplatné eval
  reference, osamocené tickety a výsledky,
- nic se automaticky nemaže; bezpečné opravy (zavření ticketu = jen status,
  normalizace tieru = bezeztrátový přepis) jen po explicitním potvrzení,
- audit data/datacheck_log.json (append-only, restart-safe) pro každou
  kontrolu i opravu.
"""

import asyncio
import json
import tempfile
import unittest
from unittest import mock

import storage
from services import datacheck
from services.datacheck import (
    canonical_tier,
    check_duplicate_players,
    check_duplicate_testers,
    check_eval_references,
    check_invalid_tiers,
    check_kit_roles_conflicts,
    check_missing_website_records,
    check_orphaned_results,
    check_orphaned_tickets,
    check_player_discord_ids,
    check_retired_tiers_in_modes,
    check_ticket_identities,
)


def _player(username, modes=None, history=None, discord_id=None):
    p = {
        "username": username,
        "modes": dict(modes or {}),
        "history": dict(history or {}),
    }
    if discord_id is not None:
        p["discordId"] = str(discord_id)
    return p


def _ticket(cid, owner="1", ign="alice", kit="AnchorPvP", status="open", **extra):
    t = {
        "id": cid,
        "status": status,
        "ownerId": owner,
        "ign": ign,
        "kit": kit,
        "targetTier": "HT3",
        "currentTier": "LT3",
        "eval": False,
        "claimerId": None,
        "members": [],
        "createdAt": 1,
    }
    t.update(extra)
    return t


def _result(rid, ign="alice", kit="AnchorPvP", new_tier="HT3", kind=None, ticket_id=None):
    r = {
        "id": rid,
        "kind": kind or ("ticket" if not str(rid).startswith("queue-") else "queue"),
        "ticketId": ticket_id,
        "playerId": "1",
        "ign": ign,
        "kit": kit,
        "newTier": new_tier,
        "timestamp": 1,
    }
    return r


class CanonicalTierTests(unittest.TestCase):
    def test_known_tiers(self):
        self.assertEqual(canonical_tier("HT3"), "HT3")
        self.assertEqual(canonical_tier(" lt3 "), "LT3")
        self.assertEqual(canonical_tier("ht4"), "HT4")
        self.assertEqual(canonical_tier("LT3 EVAL"), "LT3E")
        self.assertEqual(canonical_tier("LT3 EVALUATION"), "LT3E")
        self.assertEqual(canonical_tier("RLT2"), "RLT2")
        self.assertEqual(canonical_tier("S"), "S")
        self.assertEqual(canonical_tier("A"), "A")

    def test_unknown_tiers(self):
        self.assertIsNone(canonical_tier("XYZ"))
        self.assertIsNone(canonical_tier(""))
        self.assertIsNone(canonical_tier(None))
        self.assertIsNone(canonical_tier("HT9"))
        self.assertIsNone(canonical_tier("LT3 EVAL X"))


class PureCheckTests(unittest.TestCase):
    def test_duplicate_players(self):
        findings = check_duplicate_players(
            [
                _player("Alice"),
                _player("ALICE"),
                _player("Bob"),
                "garbage",
            ]
        )
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "duplicate_players")
        self.assertEqual(f["severity"], "error")
        self.assertIn("alice", f["message"])

    def test_duplicate_players_clean(self):
        self.assertEqual(
            check_duplicate_players([_player("Alice"), _player("Bob")]), []
        )

    def test_ticket_identities_duplicate_discord_ids(self):
        tickets = {
            "1": _ticket("1", owner="111", ign="alice"),
            "2": _ticket("2", owner="111", ign="bob"),  # stejný owner, jiné IGN
            "3": _ticket("3", owner="222", ign="cara"),
        }
        findings = check_ticket_identities(tickets)
        kinds = [f["kind"] for f in findings]
        self.assertIn("duplicate_discord_ids", kinds)
        self.assertNotIn("duplicate_ign", kinds)
        f = next(f for f in findings if f["kind"] == "duplicate_discord_ids")
        self.assertIn("111", f["message"])
        self.assertIn("alice", f["message"])

    def test_ticket_identities_duplicate_ign(self):
        tickets = {
            "1": _ticket("1", owner="111", ign="alice"),
            "2": _ticket("2", owner="222", ign="alice"),  # jedno IGN, dva účty
        }
        findings = check_ticket_identities(tickets)
        kinds = [f["kind"] for f in findings]
        self.assertIn("duplicate_ign", kinds)
        self.assertNotIn("duplicate_discord_ids", kinds)
        f = next(f for f in findings if f["kind"] == "duplicate_ign")
        self.assertIn("alice", f["message"])

    def test_duplicate_testers(self):
        findings = check_duplicate_testers(["111", "111", "222", None])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "duplicate_discord_ids")
        self.assertIn("111", findings[0]["message"])

    def test_invalid_tiers_detected_with_repair(self):
        players = [
            _player("bad", {"AnchorPvP": "XYZ"}),
            _player("spaced", {"AnchorPvP": " lt3 "}),
            _player("ok", {"AnchorPvP": "HT3"}),
        ]
        findings = check_invalid_tiers(players)
        self.assertEqual(len(findings), 2)
        by_username = {f["message"].split("**")[1]: f for f in findings}

        bad = by_username["bad"]
        self.assertEqual(bad["severity"], "warning")
        self.assertIsNone(bad["repair"])  # "XYZ" nelze bezeztrátově opravit

        spaced = by_username["spaced"]
        self.assertIsNotNone(spaced["repair"])
        self.assertEqual(spaced["repair"]["action"], "normalize_tier")
        target = spaced["repair"]["targets"][0]
        self.assertEqual(target["from"], " lt3 ")
        self.assertEqual(target["to"], "LT3")
        self.assertEqual(target["field"], "modes")

    def test_invalid_tiers_history_position(self):
        players = [
            {
                "username": "hist",
                "modes": {"AnchorPvP": "HT3"},
                "history": {
                    "AnchorPvP": [
                        {"date": "1.1.2026", "tier": "HT3"},
                        {"date": "2.2.2026", "tier": " lt3 "},
                    ]
                },
            }
        ]
        findings = check_invalid_tiers(players)
        self.assertEqual(len(findings), 1)
        target = findings[0]["repair"]["targets"][0]
        self.assertEqual(target["field"], "history")
        self.assertEqual(target["index"], 1)
        self.assertEqual(target["from"], " lt3 ")
        self.assertEqual(target["to"], "LT3")

    def test_invalid_tiers_accepts_rtier_and_tournament_letters(self):
        players = [
            _player("rl", {"AnchorPvP": "RLT2"}),
            _player("letter", {"AnchorPvP": "S", "IronAxe": "A"}),
        ]
        self.assertEqual(check_invalid_tiers(players), [])

    def test_noncanonical_tier_flagged_with_repair(self):
        players = [_player("eval", {"AnchorPvP": "LT3 EVAL"})]
        findings = check_invalid_tiers(players)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["repair"]["action"], "normalize_tier")
        target = findings[0]["repair"]["targets"][0]
        self.assertEqual(target["from"], "LT3 EVAL")
        self.assertEqual(target["to"], "LT3E")

    def test_kit_roles_conflicts(self):
        kit_roles = {
            "anchorpvp": {"HT3": "101"},
            "ironaxe": {"LT2": "101"},  # stejná role na jiný kit
            "gold": {"HT3": "202"},
        }
        findings = check_kit_roles_conflicts(kit_roles)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "conflicting_discord_roles")
        self.assertIn("101", f["message"])
        self.assertIn("anchorpvp/HT3", f["message"])

    def test_kit_roles_ignores_non_numeric_ids(self):
        kit_roles = {"anchorpvp": {"HT3": "101"}, "ironaxe": {"LT2": "101x"}}
        self.assertEqual(check_kit_roles_conflicts(kit_roles), [])

    def test_missing_website_records(self):
        players = [
            _player("missing", {"AnchorPvP": "HT3", "IronAxe": "LT2"},
                    {"AnchorPvP": [{"date": "1.1.2026", "tier": "HT3"}]}),
            _player("full", {"AnchorPvP": "HT3"},
                    {"AnchorPvP": [{"date": "1.1.2026", "tier": "HT3"}]}),
        ]
        findings = check_missing_website_records(players)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "missing_website_records")
        self.assertEqual(f["severity"], "warning")
        self.assertIn("missing", f["message"])
        self.assertIn("IronAxe", f["message"])
        self.assertIsNone(f["repair"])  # nevíme datum → jen nahlásit

    def test_missing_website_records_empty_history_bucket(self):
        players = [
            _player("empty", {"AnchorPvP": "HT3"}, {"AnchorPvP": []}),
        ]
        self.assertEqual(len(check_missing_website_records(players)), 1)

    def test_retired_tiers_in_modes(self):
        players = [
            _player("rl", {"AnchorPvP": "RLT2"}),
            _player("ok", {"AnchorPvP": "HT3"}),
        ]
        findings = check_retired_tiers_in_modes(players)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "retired_tiers_in_modes")
        self.assertEqual(f["severity"], "warning")
        self.assertIsNone(f["repair"])  # retired tier se nesmí mazat/přepisovat
        self.assertIn("rl", f["message"])

    def test_retired_tiers_in_modes_clean(self):
        self.assertEqual(
            check_retired_tiers_in_modes([_player("ok", {"AnchorPvP": "HT3"})]),
            [],
        )

    def test_player_discord_id_duplicates(self):
        players = [
            _player("One", discord_id="42"),
            _player("Two", discord_id="42"),
            _player("Three", discord_id="7"),
            _player("NoId"),
        ]
        findings = check_player_discord_ids(players)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "duplicate_player_discord_ids")
        self.assertEqual(f["severity"], "error")
        self.assertIn("42", f["message"])
        self.assertIn("One", f["message"])
        self.assertIn("Two", f["message"])

    def test_player_discord_id_clean(self):
        self.assertEqual(
            check_player_discord_ids([_player("A", discord_id="1"), _player("B")]),
            [],
        )

    def test_eval_references(self):
        players = [_player("Alice")]
        evals = {
            "anchorpvp": {"alice": 123, "ghost": 456},  # ghost není v players
            "unknownkit": {"alice": 1},
            "broken": "not-a-dict",
        }
        kits = ["AnchorPvP"]
        findings = check_eval_references(evals, players, kits)
        self.assertEqual(len(findings), 3)
        messages = "\n".join(f["message"] for f in findings)
        self.assertIn("unknownkit", messages)
        self.assertIn("ghost", messages)
        self.assertIn("není objekt", messages)

    def test_orphaned_tickets_channel_gone(self):
        tickets = {
            "111": _ticket("111", status="open"),
            "222": _ticket("222", status="closed"),
            "333": _ticket("333", status="open"),
        }
        exists = lambda cid: cid != "333"  # kanál 333 smazaný
        findings = check_orphaned_tickets(tickets, channel_exists=exists)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["kind"], "orphaned_tickets")
        self.assertEqual(f["repair"]["action"], "close_ticket")
        self.assertEqual(f["repair"]["targets"], ["333"])
        # zavřený ticket s chybějícím kanálem není problém
        self.assertNotIn("222", [x["message"] for x in findings])

    def test_orphaned_tickets_without_resolver_skips_channel_check(self):
        tickets = {"111": _ticket("111", status="open")}
        self.assertEqual(check_orphaned_tickets(tickets), [])

    def test_orphaned_tickets_structural(self):
        tickets = {
            "111": "garbage",
            "222": {"id": "222"},  # bez statusu/ownera
            "333": _ticket("333"),  # ok
        }
        findings = check_orphaned_tickets(tickets)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(f["kind"] == "orphaned_tickets" for f in findings))
        self.assertTrue(all(f["severity"] == "error" for f in findings))

    def test_orphaned_results(self):
        players = [_player("Alice")]
        tickets = {"111": _ticket("111")}
        results = {
            "111": _result("111", ticket_id="111"),                      # ok
            "222": _result("222", ticket_id="222"),                      # ticket chybí
            "queue-1-1": _result("queue-1-1", ign="ghost"),              # hráč chybí
            "queue-1-2": _result("queue-1-2", ign="ALICE", kind="queue"),  # ok
        }
        findings = check_orphaned_results(results, players, tickets)
        self.assertEqual(len(findings), 2)
        ids = {f["message"].split("**")[1] for f in findings}
        self.assertEqual(ids, {"222", "queue-1-1"})

    def test_orphaned_results_skips_non_dict(self):
        findings = check_orphaned_results({"1": "garbage"}, [_player("A")], {})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "error")


class RunDataCheckTests(unittest.TestCase):
    """End-to-end kontrola ze souborů v temp DATA_DIR + audit log."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _write(self, name, data):
        storage.save_data(name, data)

    def test_clean_data_no_findings(self):
        self._write(
            "players.json",
            [_player("Alice", {"AnchorPvP": "HT3"}, {"AnchorPvP": [{"date": "1.1.2026", "tier": "HT3"}]})],
        )
        self._write("kits.json", ["AnchorPvP"])
        self._write("testers.json", ["111"])
        self._write("kit_roles.json", {"anchorpvp": {"HT3": "101"}})

        async def main():
            report = await datacheck.run_datacheck(now=1)
            self.assertFalse(report["has_issues"])
            self.assertEqual(report["total_findings"], 0)
            self.assertEqual(report["repairable_count"], 0)
            self.assertTrue(all(v == 0 for v in report["summary"].values()))

        asyncio.run(main())

    def test_run_datacheck_finds_everything(self):
        self._write(
            "players.json",
            [
                _player("Alice", {"AnchorPvP": "XYZ"},
                        {"AnchorPvP": [{"date": "1.1.2026", "tier": "LT3"}]}),
                _player("ALICE", {"IronAxe": " lt3 "}),  # duplicita + nekanonický
                                                         # tier + bez historie
                _player("Retired", {"AnchorPvP": "RLT2"}),   # retired v modes
                _player("Dup1", discord_id="42"),            # duplicitní discordId
                _player("Dup2", discord_id="42"),
            ],
        )
        self._write("evals.json", {"nosuchkit": {"alice": 1}})
        self._write(
            "ht_tickets.json",
            {
                "1": _ticket("1", owner="111", ign="alice"),
                "2": _ticket("2", owner="111", ign="bob"),  # owner 2× jiné IGN
                "3": _ticket("3", owner="222", ign="bob"),  # IGN 2× jiný owner
                "4": _ticket("4", status="open"),           # kanál pryč
            },
        )
        self._write(
            "ht_results.json",
            {
                "4": _result("4", ticket_id="4"),
                "queue-1-1": _result("queue-1-1", ign="ghost"),
            },
        )
        self._write("kit_roles.json", {"a": {"HT3": "101"}, "b": {"LT2": "101"}})
        self._write("kits.json", ["AnchorPvP"])
        self._write("testers.json", ["111", "111"])

        exists = lambda cid: cid != "4"

        async def main():
            report = await datacheck.run_datacheck(
                channel_exists=exists, now=5,
            )
            self.assertTrue(report["has_issues"])
            # všechny kategorie (11) aspoň jednou
            for kind in datacheck.KINDS:
                self.assertGreaterEqual(
                    report["summary"][kind], 1, f"{kind} nebyl detekován"
                )
            # bezpečné opravy: zavřít ticket 4 + normalizovat " lt3 " (ALICE)
            self.assertEqual(report["repairable"]["close_ticket"], ["4"])
            self.assertGreaterEqual(len(report["repairable"]["normalize_tier"]), 1)
            target = report["repairable"]["normalize_tier"][0]
            self.assertEqual(target["to"], "LT3")

        asyncio.run(main())

    def test_run_datacheck_writes_audit_check_event(self):
        self._write("players.json", [_player("Alice")])
        self._write("kits.json", ["AnchorPvP"])

        async def main():
            report = await datacheck.run_datacheck(
                actor_id="7", actor_name="admin", now=10,
            )
            entries = await datacheck.get_datacheck_log()
            self.assertEqual(len(entries), 1)
            e = entries[0]
            self.assertEqual(e["mode"], "check")
            self.assertEqual(e["status"], "success")
            self.assertEqual(e["ts"], 10)
            self.assertEqual(e["actorId"], "7")
            self.assertEqual(e["summary"]["duplicate_players"], 0)
            self.assertIn("repairs", e)
            self.assertIsNotNone(report["ts"])

        asyncio.run(main())


class PerformRepairsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_close_orphaned_tickets_keeps_record(self):
        tickets = {
            "4": _ticket("4", status="open"),
            "5": _ticket("5", status="closed"),
        }
        storage.save_data("ht_tickets.json", tickets)

        async def main():
            result = await datacheck.perform_repairs(
                close_ticket_ids=["4", "5"], now=99,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["closed"]), 2)
            self.assertEqual(len(result["errors"]), 0)
            saved = storage.load_data("ht_tickets.json", {})
            # záznam zůstává, jen status se mění
            self.assertEqual(saved["4"]["status"], "closed")
            self.assertEqual(saved["4"]["closedAt"], 99)
            self.assertEqual(saved["5"]["status"], "closed")
            self.assertEqual(len(saved), 2)  # nic se nesmazalo

        asyncio.run(main())

    def test_close_ticket_missing_ignored_with_error(self):
        async def main():
            result = await datacheck.perform_repairs(close_ticket_ids=["nope"], now=1)
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("neexistuje", result["errors"][0]["error"])

        asyncio.run(main())

    def test_normalize_tiers_in_players(self):
        storage.save_data(
            "players.json",
            [
                _player("Alice", {"AnchorPvP": " lt3 "},
                        {"AnchorPvP": [{"date": "1.1.2026", "tier": "HT3"}]}),
                _player("Bob", {"AnchorPvP": "LT3 EVAL"}),
            ],
        )
        fixes = [
            {"username": "Alice", "kit": "AnchorPvP", "field": "modes",
             "index": None, "from": " lt3 ", "to": "LT3"},
            {"username": "Bob", "kit": "AnchorPvP", "field": "modes",
             "index": None, "from": "LT3 EVAL", "to": "LT3E"},
            {"username": "Alice", "kit": "AnchorPvP", "field": "history",
             "index": 0, "from": "ht3", "to": "HT3"},
        ]

        async def main():
            result = await datacheck.perform_repairs(tier_fixes=fixes, now=7)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["normalized"]), 3)
            players = storage.load_data("players.json", [])
            alice = next(p for p in players if p["username"] == "Alice")
            self.assertEqual(alice["modes"]["AnchorPvP"], "LT3")
            self.assertEqual(alice["history"]["AnchorPvP"][0]["tier"], "HT3")
            bob = next(p for p in players if p["username"] == "Bob")
            self.assertEqual(bob["modes"]["AnchorPvP"], "LT3E")

        asyncio.run(main())

    def test_nothing_to_repair_returns_early(self):
        async def main():
            result = await datacheck.perform_repairs(now=1)
            self.assertFalse(result["ok"])
            self.assertIn("Nic k opravě", result["message"])
            entries = await datacheck.get_datacheck_log()
            self.assertEqual(entries, [])  # nic se neloguje

        asyncio.run(main())

    def test_repair_writes_audit_event(self):
        storage.save_data("ht_tickets.json", {"4": _ticket("4", status="open")})

        async def main():
            result = await datacheck.perform_repairs(
                close_ticket_ids=["4"], actor_id="9", actor_name="admin", now=5,
            )
            self.assertTrue(result["ok"])
            entries = await datacheck.get_datacheck_log()
            self.assertEqual(len(entries), 1)
            e = entries[0]
            self.assertEqual(e["mode"], "repair")
            self.assertEqual(e["status"], "success")
            self.assertEqual(e["repairs"]["closed"], 1)
            self.assertEqual(e["actorId"], "9")

        asyncio.run(main())

    def test_corrupted_log_is_reset(self):
        storage.save_data(datacheck.DATACHECK_LOG_FILE, "garbage")

        async def main():
            await datacheck.log_datacheck_event(mode="check", status="success", ts=3)

        asyncio.run(main())
        entries = storage.load_data(datacheck.DATACHECK_LOG_FILE, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], 3)


class AuditLogSurvivalTests(unittest.TestCase):
    def test_log_survives_restart_across_loops(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(storage, "DATA_DIR", tmp):
            async def main():
                await datacheck.log_datacheck_event(mode="check", status="success", ts=1)

            asyncio.run(main())
            asyncio.run(main())  # nový loop = restart
            entries = storage.load_data(datacheck.DATACHECK_LOG_FILE, [])
            self.assertEqual(len(entries), 2)
            self.assertEqual([e["ts"] for e in entries], [1, 1])

    def test_raw_log_shape_is_json_list(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(storage, "DATA_DIR", tmp):
            async def main():
                await datacheck.log_datacheck_event(mode="check", status="success", ts=11)

            asyncio.run(main())
            import os

            with open(
                os.path.join(tmp, datacheck.DATACHECK_LOG_FILE), encoding="utf-8"
            ) as f:
                raw = json.load(f)
            self.assertIsInstance(raw, list)
            self.assertEqual(raw[0]["mode"], "check")


if __name__ == "__main__":
    unittest.main()
"""Testy rollbacku /sync discord – services/playersync (čistá logika).

Pokrývají požadavky safe-rollback specifikace:
- inverze ADD → REMOVE a REMOVE → ADD,
- akce, které původní sync NEAPLIKOVAL (ok=False), se nikdy nerollbackují,
- preview záznam se nikdy nevybere jako cíl,
- vybere se POSLEDNÍ aplikovaný sync (i když po něm běžely previews),
- explicitní výběr podle ts,
- chybějící memberId / roleId → plán odmítnut (``ok`` False),
- rollback audit se zapisuje do SEPARÁTNÍHO souboru (původní audit syncu
  se nepřepisuje),
- apply záznam bez jediné úspěšné akce není platným cílem.
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import storage
from services.playersync import (
    PLAYERSYNC_LOG_FILE,
    PLAYERSYNC_ROLLBACK_LOG_FILE,
    build_rollback_plan,
    find_rollback_target,
    get_playersync_rollback_log,
    log_playersync_rollback_event,
    verify_rollback_plan,
)


def _applied(records):
    """Apply záznam auditu (jako z apply_role_actions)."""
    return {
        "ts": 1000,
        "mode": "apply",
        "actorId": "999",
        "actorName": "boss",
        "summary": {},
        "applied": records,
    }


def _rec(op, member_id, role_id, *, ok=True, tier="HT3"):
    return {
        "op": op,
        "memberId": str(member_id),
        "memberName": "AliceMC",
        "roleId": str(role_id),
        "kit": "AnchorPvP",
        "tier": tier,
        "ok": ok,
        "error": None if ok else "forbidden",
    }


def _preview(ts=2000):
    return {
        "ts": ts,
        "mode": "preview",
        "actorId": "999",
        "actorName": "boss",
        "summary": {},
        "plannedActions": [],
    }


class RollbackPlanTests(unittest.TestCase):
    def test_add_inverts_to_remove(self):
        plan = build_rollback_plan(_applied([_rec("add", "1", "101")]))
        self.assertTrue(plan["ok"])
        self.assertEqual(len(plan["actions"]), 1)
        a = plan["actions"][0]
        self.assertEqual(a["op"], "remove")
        self.assertEqual(a["original_op"], "add")
        self.assertEqual(a["member_id"], "1")
        self.assertEqual(a["role_id"], "101")

    def test_remove_inverts_to_add(self):
        plan = build_rollback_plan(_applied([_rec("remove", "1", "101")]))
        self.assertEqual(plan["actions"][0]["op"], "add")
        self.assertEqual(plan["actions"][0]["original_op"], "remove")

    def test_failed_original_action_is_not_reversed(self):
        entry = _applied(
            [
                _rec("add", "1", "101", ok=True),
                _rec("add", "1", "102", ok=False),
                _rec("remove", "1", "103", ok=False),
            ]
        )
        plan = build_rollback_plan(entry)
        self.assertEqual(len(plan["actions"]), 1)  # jen ok=True
        self.assertEqual(plan["actions"][0]["role_id"], "101")
        self.assertEqual(plan["original_applied"], 1)
        self.assertEqual(plan["total_logged"], 3)

    def test_counts_original_vs_total(self):
        entry = _applied([_rec("add", "1", "101"), _rec("add", "1", "102", ok=False)])
        plan = build_rollback_plan(entry)
        self.assertEqual(plan["original_applied"], 1)
        self.assertEqual(plan["total_logged"], 2)

    def test_missing_member_id_rejected(self):
        entry = _applied(
            [{"op": "add", "roleId": "101", "ok": True, "error": None}]
        )
        plan = build_rollback_plan(entry)
        self.assertFalse(plan["ok"])
        self.assertEqual(len(plan["missing"]), 1)
        self.assertIn("memberId", plan["missing"][0]["reason"])

    def test_missing_role_id_rejected(self):
        entry = _applied(
            [{"op": "add", "memberId": "1", "ok": True, "error": None}]
        )
        plan = build_rollback_plan(entry)
        self.assertFalse(plan["ok"])
        self.assertEqual(len(plan["missing"]), 1)
        self.assertIn("roleId", plan["missing"][0]["reason"])

    def test_non_digit_ids_rejected(self):
        entry = _applied([_rec("add", "abc", "12x")])
        plan = build_rollback_plan(entry)
        self.assertFalse(plan["ok"])
        self.assertEqual(len(plan["missing"]), 1)

    def test_unknown_op_rejected(self):
        entry = _applied(
            [{"op": "explode", "memberId": "1", "roleId": "101", "ok": True}]
        )
        plan = build_rollback_plan(entry)
        self.assertFalse(plan["ok"])
        self.assertEqual(len(plan["missing"]), 1)

    def test_metadata_preserved(self):
        plan = build_rollback_plan(_applied([_rec("add", "1", "101")]))
        self.assertEqual(plan["target_ts"], 1000)
        self.assertEqual(plan["target_actor_id"], "999")
        self.assertEqual(plan["target_actor_name"], "boss")


class RollbackVerificationTests(unittest.TestCase):
    """Finální bezpečnostní kontrola plánu proti audit záznamu (čistá).

    Pro každou plánovanou akci ověřuje memberId + roleId + original op +
    důkaz ok=True v auditu a seskupuje souhrn podle PŮVODNÍ operace
    (REMOVE → ADD / ADD → REMOVE) s kontrolou X + Y == total.
    """

    def test_verifies_mixed_plan_and_groups_by_original(self):
        entry = _applied(
            [
                _rec("remove", "1", "101"),
                _rec("remove", "2", "102"),
                _rec("add", "3", "103"),
            ]
        )
        plan = build_rollback_plan(entry)
        v = verify_rollback_plan(entry, plan)
        self.assertTrue(v["ok"])
        self.assertTrue(v["sum_matches"])
        self.assertEqual(v["total"], 3)
        self.assertEqual(v["verified"], 3)
        self.assertEqual(v["by_original"]["remove_to_add"], 2)
        self.assertEqual(v["by_original"]["add_to_remove"], 1)
        self.assertEqual(v["problems"], [])
        x, y = v["by_original"]["remove_to_add"], v["by_original"]["add_to_remove"]
        self.assertEqual(x + y, v["total"])

    def test_missing_ok_proof_detected(self):
        # Akce je v plánu, ale v auditu neexistuje ok=True záznam (důkaz
        # úspěšné aplikace) → kontrola selže.
        entry = _applied([_rec("add", "1", "101")])
        plan = build_rollback_plan(entry)
        plan["actions"].append(
            {
                "op": "add",
                "original_op": "remove",
                "member_id": "1",
                "role_id": "999",
                "member_name": "AliceMC",
                "kit": "AnchorPvP",
                "tier": "HT3",
            }
        )
        v = verify_rollback_plan(entry, plan)
        self.assertFalse(v["ok"])
        self.assertEqual(len(v["problems"]), 1)
        self.assertIn("ok=True", v["problems"][0]["reason"])

    def test_missing_member_id_detected(self):
        entry = _applied([_rec("add", "1", "101")])
        plan = {
            "actions": [
                {
                    "op": "remove",
                    "original_op": "add",
                    "member_id": "",
                    "role_id": "101",
                }
            ]
        }
        v = verify_rollback_plan(entry, plan)
        self.assertFalse(v["ok"])
        self.assertIn("member_id", v["problems"][0]["reason"])

    def test_wrong_inversion_detected(self):
        entry = _applied([_rec("add", "1", "101")])
        plan = build_rollback_plan(entry)
        plan["actions"][0]["op"] = "add"  # poškozená inverze (má být remove)
        v = verify_rollback_plan(entry, plan)
        self.assertFalse(v["ok"])
        self.assertIn("inverz", v["problems"][0]["reason"])

    def test_sum_mismatch_reported(self):
        entry = _applied([_rec("add", "1", "101")])
        plan = {
            "actions": [
                {
                    "op": "remove",
                    "original_op": "explode",  # neplatná původní operace
                    "member_id": "1",
                    "role_id": "101",
                }
            ]
        }
        v = verify_rollback_plan(entry, plan)
        self.assertFalse(v["ok"])
        self.assertFalse(v["sum_matches"])
        self.assertEqual(v["verified"], 0)
        x, y = v["by_original"]["remove_to_add"], v["by_original"]["add_to_remove"]
        self.assertEqual(x + y, 0)

    def test_empty_plan_verified_trivially(self):
        entry = _applied([])
        plan = build_rollback_plan(entry)
        v = verify_rollback_plan(entry, plan)
        self.assertTrue(v["ok"])
        self.assertTrue(v["sum_matches"])
        self.assertEqual(v["total"], 0)
        self.assertEqual(v["verified"], 0)


class RollbackTargetSelectionTests(unittest.TestCase):
    def test_preview_entry_never_selected(self):
        entries = [_preview(ts=1), _preview(ts=2)]
        entry, warnings = find_rollback_target(entries)
        self.assertIsNone(entry)
        self.assertTrue(warnings)

    def test_last_applied_selected_even_after_previews(self):
        entries = [
            _applied([_rec("add", "1", "101")]),
            _preview(ts=2000),
            _preview(ts=3000),
        ]
        entries[0]["ts"] = 1000
        entry, _ = find_rollback_target(entries)
        self.assertEqual(entry["ts"], 1000)

    def test_oldest_of_two_applied_is_not_selected(self):
        first = _applied([_rec("add", "1", "101")])
        second = _applied([_rec("remove", "2", "202")])
        first["ts"], second["ts"] = 1000, 2000
        entry, _ = find_rollback_target([first, second])
        self.assertEqual(entry["ts"], 2000)

    def test_explicit_ts_selected(self):
        first = _applied([_rec("add", "1", "101")])
        second = _applied([_rec("remove", "2", "202")])
        first["ts"], second["ts"] = 1000, 2000
        entry, _ = find_rollback_target([first, second], target_ts=1000)
        self.assertEqual(entry["ts"], 1000)

    def test_explicit_ts_missing(self):
        entries = [_applied([_rec("add", "1", "101")])]
        entry, warnings = find_rollback_target(entries, target_ts=999)
        self.assertIsNone(entry)
        self.assertTrue(any("999" in w for w in warnings))

    def test_invalid_target_ts(self):
        entry, warnings = find_rollback_target([], target_ts="abc")
        self.assertIsNone(entry)
        self.assertTrue(warnings)

    def test_apply_without_success_is_not_target(self):
        entry = _applied([_rec("add", "1", "101", ok=False)])
        target, _ = find_rollback_target([entry])
        self.assertIsNone(target)
        target, warnings = find_rollback_target([entry], target_ts=1000)
        self.assertIsNone(target)
        self.assertTrue(any("úspěšně" in w for w in warnings))

    def test_empty_log(self):
        entry, warnings = find_rollback_target([])
        self.assertIsNone(entry)
        self.assertTrue(warnings)


class RollbackAuditLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def test_preview_audit_written_to_separate_file(self):
        async def main():
            await log_playersync_rollback_event(
                actor_id="1",
                actor_name="admin",
                mode="preview",
                target_ts=1000,
                target_actor_id="999",
                target_actor_name="boss",
                original_applied=2,
                total_logged=3,
                plan=[{"op": "add", "member_id": "1", "role_id": "101"}],
                summary={"add": 1, "remove": 1, "total": 2},
                ts=5000,
            )
            rollbacks = storage.load_data(PLAYERSYNC_ROLLBACK_LOG_FILE, [])
            self.assertEqual(len(rollbacks), 1)
            e = rollbacks[0]
            self.assertEqual(e["mode"], "preview")
            self.assertEqual(e["targetTs"], 1000)
            self.assertIn("plan", e)
            # původní audit syncu se nedotkl
            self.assertEqual(storage.load_data(PLAYERSYNC_LOG_FILE, []), [])

        asyncio.run(main())

    def test_apply_audit_records_results(self):
        async def main():
            results = [
                {
                    "op": "remove", "original_op": "add", "memberId": "1",
                    "roleId": "101", "status": "applied", "error": None,
                },
                {
                    "op": "add", "original_op": "remove", "memberId": "2",
                    "roleId": "202", "status": "already_correct", "error": None,
                },
                {
                    "op": "add", "original_op": "remove", "memberId": "3",
                    "roleId": "303", "status": "failed", "error": "Forbidden",
                },
            ]
            await log_playersync_rollback_event(
                actor_id="1",
                actor_name="admin",
                mode="apply",
                target_ts=1000,
                target_actor_id="999",
                target_actor_name="boss",
                original_applied=3,
                total_logged=4,
                results=results,
                summary={"applied": 1, "already_correct": 1, "failed": 1, "total": 3},
                ts=6000,
            )
            rollbacks = storage.load_data(PLAYERSYNC_ROLLBACK_LOG_FILE, [])
            e = rollbacks[0]
            self.assertEqual(e["mode"], "apply")
            self.assertEqual(len(e["results"]), 3)
            self.assertEqual(e["results"][2]["status"], "failed")

        asyncio.run(main())

    def test_audit_is_append_only(self):
        async def main():
            for i in range(3):
                await log_playersync_rollback_event(
                    actor_id="1", actor_name="x", mode="preview",
                    target_ts=1000, target_actor_id="999",
                    target_actor_name="boss", original_applied=1,
                    total_logged=1, plan=[], summary={}, ts=10 + i,
                )
            self.assertEqual(len(await get_playersync_rollback_log()), 3)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
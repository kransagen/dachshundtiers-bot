"""Testy synchronizace webu – services/websync.py (bez discord.py).

Pokrývají Phase 5 požadavky:
- porovnání webu (players.json z GitHubu) s kanonickou players.json
  (web je JEN kopie – synchronizace používá kanonickou DB, žádný druhý zdroj),
- detekce: chybějící hráči na webu, špatné tiery, zastaralá data, duplicitní
  hráči a neplatné záznamy,
- preview nic neposílá; aplikace vyžaduje potvrzení (fingerprint v cogu),
- retry handling čtení i zápisu,
- auditní log data/websync_log.json: timestamp, počet záznamů, úspěch/selhání
  a chyby (append-only, restart-safe).
"""

import asyncio
import copy
import tempfile
import unittest
from unittest import mock

import github_sync
import storage
from services import websync


def _player(username, modes=None, history=None):
    return {
        "username": username,
        "modes": dict(modes or {}),
        "history": dict(history or {}),
    }


def _canonical():
    return [
        _player("AliceMC", {"AnchorPvP": "HT3"}, {"AnchorPvP": [{"date": "2024-01-01", "tier": "HT3"}]}),
        _player("Bob", {"IronAxe": "LT2"}, {"IronAxe": [{"date": "2024-02-01", "tier": "LT2"}]}),
    ]


def _website():
    return [
        _player("AliceMC", {"AnchorPvP": "HT3"}, {"AnchorPvP": [{"date": "2024-01-01", "tier": "HT3"}]}),
        _player("Bob", {"IronAxe": "LT2"}, {"IronAxe": [{"date": "2024-02-01", "tier": "LT2"}]}),
    ]


class AnalyzeWebSyncTests(unittest.TestCase):
    """Čistá analýza – žádný zápis do storage, žádné discord.py."""

    def test_no_findings_when_website_matches_canonical(self):
        analysis = websync.analyze_websync(_canonical(), _website())
        self.assertFalse(analysis["findings"])
        self.assertFalse(analysis["has_issues"])
        self.assertEqual(analysis["canonical_count"], 2)
        self.assertEqual(analysis["website_count"], 2)
        self.assertEqual(analysis["summary"]["missing_player"], 0)
        self.assertEqual(analysis["summary"]["wrong_tier"], 0)
        self.assertEqual(analysis["summary"]["stale_data"], 0)
        self.assertEqual(analysis["summary"]["duplicate_player"], 0)
        self.assertEqual(analysis["summary"]["invalid_record"], 0)

    def test_missing_website_player_detected(self):
        website = _website()[:-1]  # Bob na webu chybí
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["missing_player"], 1)
        kinds = {f["kind"] for f in analysis["findings"]}
        self.assertEqual(kinds, {"missing_player"})
        f = next(f for f in analysis["findings"] if f["kind"] == "missing_player")
        self.assertEqual(f["username"], "Bob")
        self.assertIn("chybí", f["message"])

    def test_wrong_tier_detected(self):
        website = [dict(p) for p in _website()]
        website[0]["modes"]["AnchorPvP"] = "LT3"
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["wrong_tier"], 1)
        f = next(f for f in analysis["findings"] if f["kind"] == "wrong_tier")
        self.assertEqual(f["username"], "AliceMC")
        self.assertEqual(f["kit"], "AnchorPvP")
        self.assertEqual(f["canonical_tier"], "HT3")
        self.assertEqual(f["website_tier"], "LT3")

    def test_wrong_tier_when_website_missing_kit(self):
        website = [dict(p) for p in _website()]
        website[0]["modes"] = {}  # web hráče má, ale bez kitu AnchorPvP
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["wrong_tier"], 1)
        f = next(f for f in analysis["findings"] if f["kind"] == "wrong_tier")
        self.assertIsNone(f["website_tier"])
        self.assertIn("žádný tier", f["message"])

    def test_stale_data_detected_when_website_history_behind(self):
        website = _website()
        # web zná jen starší historii (1 záznam místo 2)
        website[0]["history"] = {
            "AnchorPvP": [{"date": "2024-01-01", "tier": "HT3"}],
        }
        canonical = _canonical()
        canonical[0]["history"] = {
            "AnchorPvP": [
                {"date": "2024-01-01", "tier": "HT3"},
                {"date": "2024-03-01", "tier": "HT3"},
            ],
        }
        analysis = websync.analyze_websync(canonical, website)
        self.assertEqual(analysis["summary"]["stale_data"], 1)
        f = next(f for f in analysis["findings"] if f["kind"] == "stale_data")
        self.assertEqual(f["username"], "AliceMC")
        self.assertIn("starší", f["message"])

    def test_stale_data_when_website_history_empty(self):
        canonical = _canonical()
        website = [dict(p) for p in _website()]
        website[1]["history"] = {}
        analysis = websync.analyze_websync(canonical, website)
        self.assertEqual(analysis["summary"]["stale_data"], 1)
        self.assertEqual(analysis["summary"]["wrong_tier"], 0)

    def test_duplicate_player_detected(self):
        website = _website() + [_player("ALICEMC", {"AnchorPvP": "HT3"})]
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["duplicate_player"], 1)
        f = next(f for f in analysis["findings"] if f["kind"] == "duplicate_player")
        self.assertEqual(f["username"], "AliceMC")
        self.assertEqual(f["reason"], "2×")
        self.assertIn("sloučí", f["message"])

    def test_invalid_record_non_dict(self):
        website = _website() + ["garbage", 42]
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["invalid_record"], 2)
        kinds = {f["kind"] for f in analysis["findings"]}
        self.assertEqual(kinds, {"invalid_record"})
        self.assertIn("objekt", analysis["findings"][0]["message"])

    def test_invalid_record_missing_username(self):
        website = [{"modes": {"AnchorPvP": "HT3"}}, {"username": "   "}]
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["invalid_record"], 2)
        reasons = {f["reason"] for f in analysis["findings"]}
        self.assertIn("chybí username", reasons)

    def test_invalid_record_bad_modes_and_empty_tier(self):
        website = [
            {"username": "X", "modes": "not-a-dict"},
            {"username": "Y", "modes": {"AnchorPvP": ""}},
            {"username": "Z", "modes": {"AnchorPvP": None}},
            {"username": "W", "history": "not-a-dict"},
        ]
        analysis = websync.analyze_websync(_canonical(), website)
        # kanonická AliceMC/Bob na webu nejsou → i missing_player nálezy
        self.assertEqual(analysis["summary"]["invalid_record"], 4)
        self.assertEqual(analysis["summary"]["missing_player"], 2)
        invalid = [f for f in analysis["findings"] if f["kind"] == "invalid_record"]
        self.assertEqual(len(invalid), 4)
        for f in invalid:
            self.assertEqual(f["kind"], "invalid_record")

    def test_case_insensitive_matching(self):
        website = [dict(p) for p in _website()]
        website[0]["username"] = "alicemc"
        analysis = websync.analyze_websync(_canonical(), website)
        # AliceMC se našla case-insensitive → žádný missing_player, žádné duplicity
        self.assertEqual(analysis["summary"]["missing_player"], 0)
        self.assertEqual(analysis["summary"]["duplicate_player"], 0)

    def test_lowercase_website_tier_normalized(self):
        website = [dict(p) for p in _website()]
        website[0]["modes"]["AnchorPvP"] = "ht3"
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["summary"]["wrong_tier"], 0)

    def test_website_only_players_context_not_finding(self):
        website = _website() + [_player("Ghost", {"AnchorPvP": "HT3"})]
        analysis = websync.analyze_websync(_canonical(), website)
        self.assertEqual(analysis["website_only"], ["ghost"])
        self.assertEqual(analysis["summary"]["missing_player"], 0)
        self.assertFalse(any(f["kind"] == "missing_player" for f in analysis["findings"]))

    def test_empty_canonical_yields_no_findings(self):
        analysis = websync.analyze_websync([], _website())
        self.assertFalse(analysis["has_issues"])
        self.assertEqual(analysis["canonical_count"], 0)
        self.assertEqual(analysis["website_only"], ["alicemc", "bob"])

    def test_analysis_never_mutates_inputs(self):
        canonical = _canonical()
        website = _website() + ["garbage"]
        before_c = [dict(p) for p in canonical]
        before_w = list(website)
        websync.analyze_websync(canonical, website)
        self.assertEqual(canonical, before_c)
        self.assertEqual(website, before_w)

    def test_findings_are_deterministic(self):
        web_pool = list(reversed(_website())) + ["bad", dict(_website()[0])]
        first = websync.analyze_websync(_canonical(), _website() + ["bad", dict(_website()[0])])
        for _ in range(3):
            second = websync.analyze_websync(list(reversed(_canonical())), web_pool)
            self.assertEqual(
                [f["message"] for f in first["findings"]],
                [f["message"] for f in second["findings"]],
            )


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_stable_and_sensitive(self):
        c1 = _canonical()
        c2 = copy.deepcopy(c1)
        self.assertEqual(
            websync.fingerprint_canonical(c1),
            websync.fingerprint_canonical(c2),
        )
        c2[0]["modes"]["AnchorPvP"] = "LT2"
        self.assertNotEqual(
            websync.fingerprint_canonical(c1),
            websync.fingerprint_canonical(c2),
        )

    def test_fingerprint_ignores_record_order(self):
        c1 = _canonical()
        c2 = list(reversed(c1))
        self.assertEqual(
            websync.fingerprint_canonical(c1),
            websync.fingerprint_canonical(c2),
        )


class WebSyncServiceTests(unittest.TestCase):
    """Preview/sync s mocknutým github_sync – žádný reálný HTTP."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data(websync.WEBSYNC_LOG_FILE, [])

    # --- pomocníci ------------------------------------------------------
    def _fetch_ok(self, website=None):
        return (website if website is not None else _website(), "sha1", None)

    def _push_ok(self):
        return (True, "✅ players.json na webu nahrazen kanonickou databází – web je aktuální.", _website())

    # --- preview --------------------------------------------------------
    def test_preview_without_token_is_failure_and_logged(self):
        async def main():
            with mock.patch.object(
                github_sync, "fetch_players", new=mock.AsyncMock(return_value=(None, None, None))
            ) as fetcher, mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock()
            ) as pusher:
                result = await websync.preview_website(
                    canonical=_canonical(), attempts=2, retry_delay_s=0,
                )
            self.assertFalse(result["ok"])
            self.assertIn("GITHUB_TOKEN", result["message"])
            self.assertIsNone(result["analysis"])
            fetcher.assert_awaited_once()
            pusher.assert_not_awaited()
            entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["mode"], "preview")
            self.assertEqual(entries[0]["status"], "failure")
            self.assertEqual(entries[0]["records"], 2)

        asyncio.run(main())

    def test_preview_reports_findings_and_does_not_push(self):
        website = _website()[:-1]  # Bob na webu chybí

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(website, "sha1", None)),
            ) as fetcher, mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock()
            ) as pusher:
                result = await websync.preview_website(
                    canonical=_canonical(), attempts=2, retry_delay_s=0,
                )
            self.assertTrue(result["ok"])
            self.assertTrue(result["analysis"]["has_issues"])
            self.assertEqual(result["analysis"]["summary"]["missing_player"], 1)
            self.assertTrue(result["fingerprint"])
            fetcher.assert_awaited_once()
            pusher.assert_not_awaited()  # preview se nikdy nezapisuje na web

        asyncio.run(main())

    def test_preview_ok_when_website_in_sync(self):
        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=self._fetch_ok()),
            ):
                result = await websync.preview_website(
                    canonical=_canonical(), attempts=2, retry_delay_s=0,
                )
            self.assertTrue(result["ok"])
            self.assertFalse(result["analysis"]["has_issues"])

        asyncio.run(main())

    # --- sync -----------------------------------------------------------
    def test_sync_success_records_log_with_ts_records_status(self):
        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=self._fetch_ok()),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(return_value=self._push_ok())
            ) as pusher:
                result = await websync.sync_website(
                    canonical=_canonical(),
                    message="websync: synchronizace hráčů na web",
                    attempts=2, retry_delay_s=0, now=123456,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["records"], 2)
            self.assertEqual(result["errors"], [])
            self.assertGreaterEqual(result["attempts"], 1)
            pusher.assert_awaited_once()

            entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
            self.assertEqual(len(entries), 1)
            e = entries[0]
            self.assertEqual(e["mode"], "apply")
            self.assertEqual(e["status"], "success")
            self.assertEqual(e["records"], 2)
            self.assertEqual(e["ts"], 123456)
            self.assertEqual(e["errors"], [])
            self.assertEqual(e["websiteCount"], 2)

        asyncio.run(main())

    def test_sync_empty_canonical_refuses_and_never_pushes(self):
        async def main():
            with mock.patch.object(
                github_sync, "fetch_players", new=mock.AsyncMock()
            ) as fetcher, mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock()
            ) as pusher:
                result = await websync.sync_website(
                    canonical=[], message="x", attempts=2, retry_delay_s=0,
                )
            self.assertFalse(result["ok"])
            self.assertIn("prázdná", result["message"])
            fetcher.assert_not_awaited()
            pusher.assert_not_awaited()
            entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
            self.assertEqual(entries[0]["status"], "failure")
            self.assertEqual(entries[0]["records"], 0)
            self.assertIn("kanonická DB je prázdná", entries[0]["errors"])

        asyncio.run(main())

    def test_sync_retries_fetch_until_success(self):
        async def main():
            fetch = mock.AsyncMock(
                side_effect=[(None, None, "❌ GitHub GET selhal: boom"), self._fetch_ok()]
            )
            with mock.patch.object(github_sync, "fetch_players", new=fetch), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(return_value=self._push_ok())
            ):
                result = await websync.sync_website(
                    canonical=_canonical(), message="m", attempts=3, retry_delay_s=0,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(fetch.await_count, 2)  # 1. selhal, 2. úspěch
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("GET selhal", result["errors"][0])

        asyncio.run(main())

    def test_sync_retries_push_on_transient_failure(self):
        push_results = [
            (False, "❌ GitHub zápis selhal (500)", None),
            self._push_ok(),
        ]

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=self._fetch_ok()),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(side_effect=push_results)
            ) as pusher:
                result = await websync.sync_website(
                    canonical=_canonical(), message="m", attempts=3, retry_delay_s=0,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(pusher.await_count, 2)  # 1. selhal (500), 2. úspěch
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn("(500)", result["errors"][0])
            self.assertEqual(result["attempts"], 3)  # 1 fetch + 2 pushy

        asyncio.run(main())

    def test_sync_push_no_token_does_not_retry(self):
        push_results = [
            (False, "⚠️ GITHUB_TOKEN není nastaven – uloženo jen lokálně (web se nezmění).", None),
        ]

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=self._fetch_ok()),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(side_effect=push_results)
            ) as pusher:
                result = await websync.sync_website(
                    canonical=_canonical(), message="m", attempts=3, retry_delay_s=0,
                )
            self.assertFalse(result["ok"])
            self.assertIn("GITHUB_TOKEN", result["message"])
            pusher.assert_awaited_once()  # bez tokenu retry nedává smysl
            entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
            self.assertEqual(entries[0]["status"], "failure")
            self.assertIn("GITHUB_TOKEN", entries[0]["errors"][0])

        asyncio.run(main())

    def test_sync_push_build_fn_replaces_website_with_canonical(self):
        captured = {}

        async def fake_push(message, build_fn, **kwargs):
            # build_fn dostane JINÝ seznam (třeba starší web) a musí vrátit
            # kanonickou DB jako kopii („use the canonical player database“).
            built = build_fn([{"username": "OldWeb", "modes": {"AnchorPvP": "HT9"}}])
            captured["built"] = built
            return (True, "✅ ok", built)

        async def main():
            canonical = _canonical()
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=self._fetch_ok()),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(side_effect=fake_push)
            ):
                result = await websync.sync_website(
                    canonical=canonical, message="m", attempts=2, retry_delay_s=0,
                )
            self.assertTrue(result["ok"])
            built = captured["built"]
            self.assertEqual([p["username"] for p in built], ["AliceMC", "Bob"])
            self.assertNotIn("OldWeb", [p["username"] for p in built])
            # kopie, ne ten samý seznam (merge funkce ho nesmí měnit)
            self.assertNotEqual(id(built), id(canonical))
            canonical[0]["modes"]["AnchorPvP"] = "LT2"
            self.assertEqual(built[0]["modes"]["AnchorPvP"], "HT3")  # hloubková kopie

        asyncio.run(main())

    def test_sync_failure_after_all_push_attempts(self):
        push_results = [
            (False, "❌ GitHub zápis selhal (500)", None),
            (False, "❌ GitHub zápis selhal (503)", None),
        ]

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=self._fetch_ok()),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(side_effect=push_results)
            ) as pusher:
                result = await websync.sync_website(
                    canonical=_canonical(), message="m", attempts=2, retry_delay_s=0,
                )
            self.assertFalse(result["ok"])
            self.assertEqual(pusher.await_count, 2)
            self.assertEqual(len(result["errors"]), 2)
            entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
            self.assertEqual(entries[0]["status"], "failure")
            self.assertEqual(len(entries[0]["errors"]), 2)

        asyncio.run(main())


class WebSyncAuditLogTests(unittest.TestCase):
    """Auditní log data/websync_log.json (append-only, restart-safe)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data(websync.WEBSYNC_LOG_FILE, [])

    def test_log_survives_restart_across_loops(self):
        async def main():
            await websync.log_websync_event(
                actor_id="1", actor_name="admin", mode="apply",
                status="success", records=2, ts=1,
            )

        asyncio.run(main())
        asyncio.run(main())  # nový event loop = simulace restartu
        entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
        self.assertEqual(len(entries), 2)

    def test_log_entries_are_append_only(self):
        async def main():
            await websync.log_websync_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", records=2, ts=1,
            )
            for i in range(3):
                await websync.log_websync_event(
                    actor_id="1", actor_name="admin", mode="apply",
                    status="success" if i % 2 == 0 else "failure",
                    records=2, errors=[f"err{i}"] if i % 2 else [],
                    ts=10 + i,
                )

        asyncio.run(main())
        entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
        self.assertEqual([e["ts"] for e in entries], [1, 10, 11, 12])
        self.assertEqual(entries[1]["status"], "success")
        self.assertEqual(entries[2]["status"], "failure")
        self.assertEqual(entries[2]["errors"], ["err1"])

    def test_log_entry_contains_required_fields(self):
        async def main():
            entry = await websync.log_websync_event(
                actor_id="42", actor_name="boss", mode="apply",
                status="success", records=7,
                website_count=5, findings={"missing_player": 2},
                errors=[], attempts=3, ts=99,
            )
            log_entries = await websync.get_websync_log()
            self.assertEqual(len(log_entries), 1)
            saved = log_entries[0]
            for key in ("ts", "records", "status", "mode", "errors"):
                self.assertIn(key, saved)
            self.assertEqual(saved["ts"], 99)
            self.assertEqual(saved["records"], 7)
            self.assertEqual(saved["status"], "success")
            self.assertEqual(saved["findings"]["missing_player"], 2)
            self.assertEqual(saved["errors"], [])
            self.assertEqual(saved["attempts"], 3)
            self.assertEqual(entry["actorId"], "42")
            self.assertEqual(entry["actorName"], "boss")

        asyncio.run(main())

    def test_corrupted_log_is_reset_instead_of_crash(self):
        storage.save_data(websync.WEBSYNC_LOG_FILE, "not-a-list")

        async def main():
            await websync.log_websync_event(
                actor_id="1", actor_name="admin", mode="apply",
                status="success", records=2, ts=5,
            )

        asyncio.run(main())
        entries = storage.load_data(websync.WEBSYNC_LOG_FILE, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], 5)

    def test_get_websync_log_skips_non_dict_entries(self):
        async def main():
            await websync.log_websync_event(
                actor_id="1", actor_name="admin", mode="preview",
                status="success", records=1, ts=1,
            )

        asyncio.run(main())
        with open(storage.data_path(websync.WEBSYNC_LOG_FILE), "r", encoding="utf-8") as f:
            import json
            raw = json.load(f)
        raw.append("garbage")
        storage.save_data(websync.WEBSYNC_LOG_FILE, raw)

        async def read():
            return await websync.get_websync_log()

        entries = asyncio.run(read())
        self.assertEqual(len(entries), 1)
        self.assertTrue(all(isinstance(e, dict) for e in entries))


if __name__ == "__main__":
    unittest.main()
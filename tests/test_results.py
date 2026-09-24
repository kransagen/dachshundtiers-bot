"""Testy „unified HT result system" – services/results.py (bez discord.py).

Pokrývají Phase 3 požadavky:
- propojení výsledku s hráčem / Discord ID / IGN / evaluátorem / ticketem /
  časem / předchozím tierem / novým tierem / poznámkami,
- validaci výsledků (tier vs. cíl ticketu, vlastník, kit, otevřený ticket),
- idempotenci (HT ticket = max. 1 výsledek; opakované odeslání vrátí stejný
  záznam a nic nepřepíše),
- ochranu před duplicitami (queue výsledek přes aktivní cooldown hráče),
- zachování historie (data/ht_results.json, append-only),
- aktualizaci kanonické players.json po potvrzení,
- zavření ticketu + HT3+ cooldown + event log při ticket výsledku,
- souběžné zápisy (race) a restart-safe stav napříč event loopy.
"""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import storage
from cogs.results import Results
from services import results, tickets

NOW = 1_700_000_000_000
QUEUE_COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000  # 4 dny, jako PLAYER_COOLDOWN_MS
HT3_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000  # 7 dní, jako HT3_COOLDOWN_MS

INVALID_QUEUE_TIER_MSG = (
    "❌ Neplatný tier! V `/result` lze zadat pouze: "
    "**LT5, HT5, LT4, HT4, LT3, LT3 + eval**."
)


def _ticket(**overrides):
    """Sestaví otevřený HT ticket (bez zápisu) pro testy."""
    base = tickets.make_ticket(
        channel_id=1001,
        owner_id="1",
        owner_name="alice",
        ign="AliceMC",
        kit="AnchorPvP",
        target_tier="HT3",
        current_tier="LT3",
        eval_ok=True,
        category_id=555,
        now=NOW,
    )
    base.update(overrides)
    return base


class ValidateResultTierTests(unittest.TestCase):
    """Čistá validace tieru – queue i HT ticket cesta."""

    def test_queue_tiers_accepted(self):
        for tier in ("LT5", "HT5", "LT4", "HT4", "LT3", "LT3E"):
            ok, msg = results.validate_result_tier(tier)
            self.assertTrue(ok, msg)

    def test_queue_rejects_ht3_and_above_with_exact_message(self):
        for tier in ("HT3", "LT2", "HT2", "LT1", "HT1", "rubbish", ""):
            ok, msg = results.validate_result_tier(tier)
            self.assertFalse(ok, tier)
            self.assertEqual(msg, INVALID_QUEUE_TIER_MSG, tier)

    def test_ticket_accepts_target_and_confirm(self):
        # cíl HT3, aktuální LT3 (retest) → dovnitř jdou HT3 a LT3
        self.assertTrue(results.validate_result_tier("HT3", target_tier="HT3", current_tier="LT3")[0])
        self.assertTrue(results.validate_result_tier("LT3", target_tier="HT3", current_tier="LT3")[0])
        # LT3E holder (aktuální LT3E) si může nechat potvrdit eval
        self.assertTrue(results.validate_result_tier("LT3E", target_tier="HT3", current_tier="LT3E")[0])
        self.assertTrue(results.validate_result_tier("HT3", target_tier="HT3", current_tier="LT3E")[0])

    def test_ticket_rejects_better_than_target(self):
        ok, msg = results.validate_result_tier("LT2", target_tier="HT3", current_tier="LT3")
        self.assertFalse(ok)
        self.assertIn("lepší než cíl ticketu", msg)

    def test_ticket_rejects_downgrade(self):
        ok, msg = results.validate_result_tier("LT4", target_tier="HT3", current_tier="LT3")
        self.assertFalse(ok)
        self.assertIn("nedegraduje", msg)

    def test_ticket_rejects_unknown_tier(self):
        ok, msg = results.validate_result_tier("RX", target_tier="HT3", current_tier="LT3")
        self.assertFalse(ok)
        self.assertIn("Neplatný tier", msg)

    def test_ticket_without_current_allows_any_up_to_target(self):
        self.assertTrue(results.validate_result_tier("LT5", target_tier="HT3", current_tier=None)[0])
        self.assertTrue(results.validate_result_tier("HT3", target_tier="HT3", current_tier=None)[0])
        self.assertFalse(results.validate_result_tier("HT1", target_tier="HT3", current_tier=None)[0])

    def test_top_target(self):
        self.assertTrue(results.validate_result_tier("HT1", target_tier="HT1", current_tier="LT2")[0])


class ApplyResultToPlayersTests(unittest.TestCase):
    """Aplikace výsledku na kanonickou players.json (čistá funkce)."""

    def test_new_player_created(self):
        players, prev = results.apply_result_to_players([], "AliceMC", "AnchorPvP", "LT3", "23.09.2026")
        self.assertEqual(prev, "N/A")
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["modes"]["AnchorPvP"], "LT3")
        self.assertEqual(players[0]["history"]["AnchorPvP"], [{"date": "23.09.2026", "tier": "LT3"}])

    def test_existing_player_previous_tier_and_history(self):
        src = [{"username": "AliceMC", "modes": {"AnchorPvP": "LT3"}, "history": {"AnchorPvP": []}}]
        players, prev = results.apply_result_to_players(src, "AliceMC", "AnchorPvP", "HT3", "24.09.2026")
        self.assertEqual(prev, "LT3")
        self.assertEqual(players[0]["modes"]["AnchorPvP"], "HT3")
        self.assertEqual(players[0]["history"]["AnchorPvP"][-1], {"date": "24.09.2026", "tier": "HT3"})
        # čistá funkce – vstup se nemění
        self.assertEqual(src[0]["modes"]["AnchorPvP"], "LT3")

    def test_eval_stored_as_lt3(self):
        players, _ = results.apply_result_to_players([], "alice", "AnchorPvP", "LT3", "23.09.2026")
        self.assertEqual(players[0]["modes"]["AnchorPvP"], "LT3")


class RecordResultQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            ("players.json", []),
            ("cooldowns.json", {}),
            (results.HT_RESULTS_FILE, {}),
            (tickets.HT_TICKETS_FILE, {}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
        ):
            storage.save_data(name, default)

    async def _record(self, **overrides):
        kwargs = dict(
            player_id="1",
            player_name="alice",
            ign="AliceMC",
            evaluator_id="9",
            evaluator_name="bob",
            kit="AnchorPvP",
            new_tier="LT3",
            display_tier="LT3",
            score="5-2",
            outcome="Won",
            notes="Solidní hra",
            now=NOW,
            date="23.09.2026",
            queue_cooldown_ms=QUEUE_COOLDOWN_MS,
        )
        kwargs.update(overrides)
        return await results.record_result(**kwargs)

    def test_queue_result_connected_to_all_fields(self):
        async def main():
            rec = await self._record()
            self.assertEqual(rec["result"], "created")
            data = rec["record"]
            self.assertEqual(data["kind"], "queue")
            self.assertIsNone(data["ticketId"])
            self.assertEqual(data["playerId"], "1")  # Discord ID
            self.assertEqual(data["playerName"], "alice")
            self.assertEqual(data["ign"], "AliceMC")
            self.assertEqual(data["evaluatorId"], "9")
            self.assertEqual(data["evaluatorName"], "bob")
            self.assertEqual(data["timestamp"], NOW)
            self.assertEqual(data["date"], "23.09.2026")
            self.assertEqual(data["previousTier"], "N/A")
            self.assertEqual(data["newTier"], "LT3")
            self.assertEqual(data["notes"], "Solidní hra")
            self.assertEqual(data["score"], "5-2")
            self.assertEqual(data["outcome"], "Won")

            # kanonická players.json a cooldown
            players = storage.load_data("players.json", [])
            self.assertEqual(players[0]["modes"]["AnchorPvP"], "LT3")
            cds = storage.load_data("cooldowns.json", {})
            self.assertEqual(cds["1"], NOW)
        asyncio.run(main())

    def test_queue_result_previous_tier_from_canonical_db(self):
        async def main():
            await self._record()
            rec = await self._record(
                player_id="1", ign="AliceMC", new_tier="HT4", display_tier="HT4",
                now=NOW + QUEUE_COOLDOWN_MS + 1, date="27.09.2026",
            )
            self.assertEqual((rec["result"], rec["record"]["previousTier"]), ("created", "LT3"))
            self.assertEqual(rec["previous_tier"], "LT3")
        asyncio.run(main())

    def test_queue_duplicate_blocked_by_active_cooldown(self):
        async def main():
            first = await self._record()
            self.assertEqual(first["result"], "created")
            second = await self._record(
                new_tier="HT3", display_tier="HT3", now=NOW + 1,
            )
            self.assertEqual(second["result"], "duplicate")
            self.assertIsNotNone(second.get("existing"))
            # nic se nepřepsalo – kanonická DB pořád LT3, historie 1 záznam
            players = storage.load_data("players.json", [])
            self.assertEqual(players[0]["modes"]["AnchorPvP"], "LT3")
            hist = storage.load_data(results.HT_RESULTS_FILE, {})
            self.assertEqual(len(hist), 1)
        asyncio.run(main())

    def test_queue_duplicate_after_cooldown_expiry_is_new_result(self):
        async def main():
            await self._record()
            second = await self._record(
                now=NOW + QUEUE_COOLDOWN_MS + 1, date="27.09.2026",
            )
            self.assertEqual(second["result"], "created")
            hist = storage.load_data(results.HT_RESULTS_FILE, {})
            self.assertEqual(len(hist), 2)
        asyncio.run(main())

    def test_queue_invalid_tier_writes_nothing(self):
        async def main():
            rec = await self._record(new_tier="HT3")
            self.assertEqual(rec["result"], "invalid_tier")
            self.assertEqual(rec["message"], INVALID_QUEUE_TIER_MSG)
            self.assertEqual(storage.load_data("players.json", []), [])
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
            self.assertEqual(storage.load_data("cooldowns.json", {}), {})
        asyncio.run(main())

    def test_queue_notes_optional(self):
        async def main():
            rec = await self._record(notes="   ")
            self.assertIsNone(rec["record"]["notes"])
        asyncio.run(main())


class RecordResultTicketTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            ("players.json", [{"username": "AliceMC", "modes": {"AnchorPvP": "LT3"}, "history": {"AnchorPvP": []}}]),
            ("cooldowns.json", {}),
            (results.HT_RESULTS_FILE, {}),
            (tickets.HT_TICKETS_FILE, {"1001": _ticket()}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
        ):
            storage.save_data(name, default)

    async def _record(self, **overrides):
        kwargs = dict(
            ticket_id=1001,
            player_id="1",
            player_name="alice",
            ign="AliceMC",
            evaluator_id="9",
            evaluator_name="bob",
            kit="AnchorPvP",
            new_tier="HT3",
            display_tier="HT3",
            score="5-2",
            outcome="Won",
            notes="HT3 pass",
            now=NOW,
            date="23.09.2026",
            queue_cooldown_ms=QUEUE_COOLDOWN_MS,
            ht3_cooldown_ms=HT3_COOLDOWN_MS,
        )
        kwargs.update(overrides)
        return await results.record_result(**kwargs)

    def test_ticket_result_connected_and_closes_ticket(self):
        async def main():
            rec = await self._record()
            self.assertEqual(rec["result"], "created")
            data = rec["record"]
            self.assertEqual(data["kind"], "ticket")
            self.assertEqual(data["ticketId"], "1001")
            self.assertEqual(data["playerId"], "1")
            self.assertEqual(data["evaluatorId"], "9")
            self.assertEqual(data["ign"], "AliceMC")
            self.assertEqual(data["newTier"], "HT3")
            self.assertEqual(data["previousTier"], "LT3")  # z players.json
            self.assertEqual(data["notes"], "HT3 pass")

            # kanonická players.json se posunula
            players = storage.load_data("players.json", [])
            self.assertEqual(players[0]["modes"]["AnchorPvP"], "HT3")

            # ticket je zavřený + HT3+ cooldown + event log
            ticket = storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]
            self.assertEqual(ticket["status"], "closed")
            self.assertEqual(ticket["closedAt"], NOW)
            cds = storage.load_data(tickets.HT3_COOLDOWNS_FILE, {})
            self.assertEqual(cds["1"]["AnchorPvP"], NOW + HT3_COOLDOWN_MS)
            logs = storage.load_data(tickets.HT_TICKET_LOGS_FILE, {})["1001"]
            self.assertEqual(logs[-1]["action"], "result")
            self.assertEqual(logs[-1]["details"], "LT3 → HT3")
            self.assertEqual(logs[-1]["actorId"], "9")

            # queue cooldown hráče taky
            self.assertEqual(storage.load_data("cooldowns.json", {})["1"], NOW)
        asyncio.run(main())

    def test_ticket_result_idempotent(self):
        async def main():
            first = await self._record()
            self.assertEqual(first["result"], "created")
            second = await self._record(now=NOW + 1000)
            self.assertEqual(second["result"], "duplicate")
            self.assertEqual(second["existing"]["id"], "1001")
            # nic se neduplikuje: 1 výsledek, 1 history záznam, cooldown stejný
            hist = storage.load_data(results.HT_RESULTS_FILE, {})
            self.assertEqual(len(hist), 1)
            players = storage.load_data("players.json", [])
            self.assertEqual(len(players[0]["history"]["AnchorPvP"]), 1)
            cds = storage.load_data(tickets.HT3_COOLDOWNS_FILE, {})
            self.assertEqual(cds["1"]["AnchorPvP"], NOW + HT3_COOLDOWN_MS)
        asyncio.run(main())

    def test_ticket_result_rejects_closed_ticket(self):
        async def main():
            await tickets.close_ticket("1001", "9", cooldown_ms=0, now=NOW)
            rec = await self._record()
            self.assertEqual(rec["result"], "ticket_closed")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_ticket_result_rejects_wrong_player(self):
        async def main():
            rec = await self._record(player_id="2", player_name="carol")
            self.assertEqual(rec["result"], "wrong_player")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_ticket_result_rejects_wrong_kit(self):
        async def main():
            rec = await self._record(kit="MolePVP")
            self.assertEqual(rec["result"], "wrong_kit")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_ticket_result_rejects_tier_beyond_target(self):
        async def main():
            rec = await self._record(new_tier="LT2", display_tier="LT2")
            self.assertEqual(rec["result"], "invalid_tier")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
            players = storage.load_data("players.json", [])
            self.assertEqual(players[0]["modes"]["AnchorPvP"], "LT3")  # beze změny
        asyncio.run(main())

    def test_ticket_result_not_found(self):
        async def main():
            rec = await self._record(ticket_id=9999)
            self.assertEqual(rec["result"], "not_found")
        asyncio.run(main())


class ResultHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            ("players.json", []),
            ("cooldowns.json", {}),
            (results.HT_RESULTS_FILE, {}),
            (tickets.HT_TICKETS_FILE, {"1001": _ticket()}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
        ):
            storage.save_data(name, default)

    def test_history_per_player_chronological(self):
        async def main():
            await results.record_result(
                ticket_id=1001, player_id="1", player_name="alice", ign="AliceMC",
                evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
                new_tier="HT3", display_tier="HT3", score="5-2", outcome="Won",
                now=NOW, date="23.09.2026",
                queue_cooldown_ms=QUEUE_COOLDOWN_MS, ht3_cooldown_ms=HT3_COOLDOWN_MS,
            )
            await results.record_result(
                player_id="1", player_name="alice", ign="AliceMC",
                evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
                new_tier="LT3", display_tier="LT3", score="0-3", outcome="Lost",
                now=NOW + QUEUE_COOLDOWN_MS + 1, date="27.09.2026",
                queue_cooldown_ms=QUEUE_COOLDOWN_MS,
            )
            await results.record_result(
                player_id="2", player_name="carol", ign="CarolMC",
                evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
                new_tier="LT4", display_tier="LT4", score="3-3", outcome="Won",
                now=NOW + 2, date="23.09.2026",
                queue_cooldown_ms=QUEUE_COOLDOWN_MS,
            )
            by_player = await results.get_results_for_player("1")
            self.assertEqual(len(by_player), 2)
            self.assertEqual(by_player[0]["newTier"], "HT3")
            self.assertEqual(by_player[1]["previousTier"], "HT3")
            by_ticket = await results.get_result_by_ticket(1001)
            self.assertEqual(by_ticket["ticketId"], "1001")
            self.assertIsNone(await results.get_result_by_ticket(777))
            all_results = await results.get_all_results()
            self.assertEqual(len(all_results), 3)
        asyncio.run(main())

    def test_history_survives_restart_across_event_loops(self):
        # zápis v jednom loopu…
        async def record():
            return await results.record_result(
                player_id="1", player_name="alice", ign="AliceMC",
                evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
                new_tier="LT3", display_tier="LT3", score="5-2", outcome="Won",
                now=NOW, date="23.09.2026",
                queue_cooldown_ms=QUEUE_COOLDOWN_MS,
            )
        asyncio.run(record())

        # …čtení + nový zápis v úplně jiném loopu (restart bota)
        async def read_and_record_again():
            hist = await results.get_results_for_player("1")
            self.assertEqual(len(hist), 1)
            second = await results.record_result(
                player_id="1", player_name="alice", ign="AliceMC",
                evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
                new_tier="HT4", display_tier="HT4", score="5-1", outcome="Won",
                now=NOW + QUEUE_COOLDOWN_MS + 1, date="27.09.2026",
                queue_cooldown_ms=QUEUE_COOLDOWN_MS,
            )
            self.assertEqual(second["result"], "created")
            self.assertEqual(second["record"]["previousTier"], "LT3")
        asyncio.run(read_and_record_again())


class RecordResultConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            ("players.json", []),
            ("cooldowns.json", {}),
            (results.HT_RESULTS_FILE, {}),
            (tickets.HT_TICKETS_FILE, {"1001": _ticket()}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
        ):
            storage.save_data(name, default)

    def test_two_concurrent_ticket_results_one_wins(self):
        kwargs = dict(
            player_id="1", player_name="alice", ign="AliceMC",
            evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
            new_tier="HT3", display_tier="HT3", score="5-2", outcome="Won",
            now=NOW, date="23.09.2026",
            queue_cooldown_ms=QUEUE_COOLDOWN_MS, ht3_cooldown_ms=HT3_COOLDOWN_MS,
        )

        async def main():
            r1, r2 = await asyncio.gather(
                results.record_result(ticket_id=1001, **kwargs),
                results.record_result(ticket_id=1001, **kwargs),
            )
            results_list = sorted((r1["result"], r2["result"]))
            self.assertEqual(results_list, ["created", "duplicate"])
            # historie má přesně jeden záznam
            self.assertEqual(len(storage.load_data(results.HT_RESULTS_FILE, {})), 1)
        asyncio.run(main())

    def test_two_concurrent_queue_results_same_player_one_wins(self):
        kwargs = dict(
            player_id="1", player_name="alice", ign="AliceMC",
            evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
            new_tier="LT3", display_tier="LT3", score="5-2", outcome="Won",
            now=NOW, date="23.09.2026",
            queue_cooldown_ms=QUEUE_COOLDOWN_MS,
        )

        async def main():
            r1, r2 = await asyncio.gather(
                results.record_result(**kwargs),
                results.record_result(**kwargs),
            )
            results_list = sorted((r1["result"], r2["result"]))
            self.assertEqual(results_list, ["created", "duplicate"])
            self.assertEqual(len(storage.load_data(results.HT_RESULTS_FILE, {})), 1)
            players = storage.load_data("players.json", [])
            self.assertEqual(len(players[0]["history"]["AnchorPvP"]), 1)
        asyncio.run(main())

    def test_concurrent_results_different_players_both_created(self):
        async def main():
            base = dict(
                evaluator_id="9", evaluator_name="bob", kit="AnchorPvP",
                new_tier="LT3", display_tier="LT3", score="5-2", outcome="Won",
                now=NOW, date="23.09.2026", queue_cooldown_ms=QUEUE_COOLDOWN_MS,
            )
            r1 = await results.record_result(
                player_id="1", player_name="alice", ign="AliceMC", **base
            )
            r2 = await results.record_result(
                player_id="2", player_name="carol", ign="CarolMC", **base
            )
            self.assertEqual((r1["result"], r2["result"]), ("created", "created"))
            self.assertEqual(len(storage.load_data(results.HT_RESULTS_FILE, {})), 2)
        asyncio.run(main())


class ResultCogSelfResultTests(unittest.TestCase):
    """item 6: tester si NEMŮŽE zapsat výsledek sám sobě (admin ano)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _interaction(self, user_id):
        inter = mock.MagicMock()
        inter.user = SimpleNamespace(
            id=user_id,
            name="tester",
            display_name="tester",
            roles=[SimpleNamespace(id=777, name="Tester")],
            guild_permissions=SimpleNamespace(administrator=False),
        )
        inter.guild = mock.MagicMock()
        inter.channel = mock.MagicMock()  # mimo HT ticket → queue cesta
        inter.response.send_message = mock.AsyncMock()
        inter.response.defer = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        return inter

    def _call(self, cog, inter, hrac):
        async def main():
            await cog.result.callback(
                cog,
                interaction=inter,
                hrac=hrac,
                ign="AliceMC",
                kit="AnchorPvP",
                tier="LT3",
                score="3:1",
                outcome="WON",
            )

        asyncio.run(main())

    def test_tester_cannot_record_own_result(self):
        cog = Results.__new__(Results)
        inter = self._interaction(user_id=777)
        hrac = SimpleNamespace(id=777, name="AliceMC", display_name="AliceMC")

        with mock.patch("cogs.results.has_tester_role", return_value=True), \
             mock.patch("cogs.results.has_admin_role", return_value=False):
            self._call(cog, inter, hrac)

        inter.response.send_message.assert_awaited_once_with(
            "❌ Nemůžeš zapisovat výsledek sám sobě.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()

    def test_tester_can_record_other_players_result(self):
        """Jiný hráč → guard propustí do hlavního toku (defer → validace)."""
        cog = Results.__new__(Results)
        inter = self._interaction(user_id=777)
        hrac = SimpleNamespace(id=999, name="BobMC", display_name="BobMC")

        with mock.patch("cogs.results.has_tester_role", return_value=True), \
             mock.patch("cogs.results.has_admin_role", return_value=False), \
             mock.patch(
                 "cogs.results.validate_result_tier",
                 return_value=(False, "test-stop"),
             ):
            self._call(cog, inter, hrac)

        # guard prošel → flow pokročil za self-result kontrolu
        inter.response.defer.assert_awaited_once()
        self.assertEqual(
            inter.response.send_message.await_count, 0,
            "self-result hláška se nesmí poslat",
        )
        inter.followup.send.assert_awaited_once_with("test-stop", ephemeral=True)

    def test_admin_may_record_own_result(self):
        """Admin (jiný subjekt dohledu) smí zapsat i sobě."""
        cog = Results.__new__(Results)
        inter = self._interaction(user_id=777)
        hrac = SimpleNamespace(id=777, name="AliceMC", display_name="AliceMC")

        with mock.patch("cogs.results.has_tester_role", return_value=True), \
             mock.patch("cogs.results.has_admin_role", return_value=True), \
             mock.patch(
                 "cogs.results.validate_result_tier",
                 return_value=(False, "test-stop"),
             ):
            self._call(cog, inter, hrac)

        inter.response.defer.assert_awaited_once()
        self.assertEqual(inter.response.send_message.await_count, 0)


class CanonicalKitKeyTests(unittest.TestCase):
    """item 8: apply_result_to_players píše modes/history pod kanonickým
    (display-case) názvem kitu z kits.json – žádné case-duplicitní klíče."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data("kits.json", ["MolePVP"])

    def test_new_result_written_under_canonical_case(self):
        players, prev = results.apply_result_to_players(
            [], "alice", "molepvp", "LT3", "23.09.2026"
        )
        self.assertEqual(prev, "N/A")
        self.assertIn("MolePVP", players[0]["modes"])
        self.assertNotIn("molepvp", players[0]["modes"])
        self.assertEqual(players[0]["modes"]["MolePVP"], "LT3")
        self.assertEqual(players[0]["history"]["MolePVP"][-1]["tier"], "LT3")

    def test_existing_variant_key_migrated_without_data_loss(self):
        src = [{
            "username": "alice",
            "modes": {"molepvp": "LT3"},
            "history": {"molepvp": [{"date": "01.01.2026", "tier": "LT3"}]},
        }]
        players, prev = results.apply_result_to_players(
            src, "alice", "molepvp", "HT3", "24.09.2026"
        )
        self.assertEqual(prev, "LT3")  # stará hodnota se našla přes migraci
        self.assertEqual(players[0]["modes"], {"MolePVP": "HT3"})
        self.assertEqual(
            players[0]["history"]["MolePVP"],
            [
                {"date": "01.01.2026", "tier": "LT3"},
                {"date": "24.09.2026", "tier": "HT3"},
            ],
        )
        self.assertNotIn("molepvp", players[0]["history"])

    def test_never_creates_duplicate_keys_for_same_kit(self):
        src = [{
            "username": "alice",
            "modes": {"MolePVP": "HT3"},
            "history": {"MolePVP": [{"date": "01.01.2026", "tier": "HT3"}]},
        }]
        players, prev = results.apply_result_to_players(
            src, "alice", "molepvp", "LT3", "25.09.2026"
        )
        self.assertEqual(prev, "HT3")
        self.assertEqual(list(players[0]["modes"].keys()), ["MolePVP"])
        self.assertEqual(list(players[0]["history"].keys()), ["MolePVP"])


if __name__ == "__main__":
    unittest.main()
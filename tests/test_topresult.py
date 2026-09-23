"""Testy HT Fight výsledků – /topresult (services/topresult.py, bez discord.py).

Pokrývají zadání /topresult:
- /topresult NENÍ leaderboard – je to specializovaná verze /result pro HT Fighty,
- správný formát zprávy (přesně styl serveru včetně role mentionu <@&id>),
- result_type = ht_fight ve stejné kanonické historii (ht_results.json),
- TOP_RESULT_CHANNEL_ID / TOP_RESULT_ROLE_ID (konfigurace /topresult),
- role ping se generuje z nakonfigurovaného ID (<@&ROLE_ID>),
- neplatný hráč / kit / HT tier / skóre / status,
- /topresult mimo HT ticket a uvnitř HT Fight ticketu (metadata z ticketu),
- duplicitní odeslání (idempotence ticketId + result_type),
- /topresult NEPOUŽÍVÁ běžný výsledkový kanál (jen TOP_RESULT_CHANNEL_ID),
- /topresult NEMĚNÍ tier hráče (players.json se nedotýká) – změna tieru
  po HT Fightu by musela projít kanonickou logikou /result.
Oprávnění (tester role) se hlídá v cogy přes has_tester_role – stejně jako
/result (tyto čisté testy discord.py nespouštějí; test_permissions.py pokrývá
samotnou permisní logiku).
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import storage
from services import results, tickets
from services import topresult

try:
    import config
except Exception:  # noqa: BLE001  (config se čte i bez dotenv)
    config = None

NOW = 1_700_000_000_000

# Příklad ze zadání („CURRENT FORMAT USED BY THE SERVER"):
PLAYER_MENTION = 1419031701920940163
OPPONENT_MENTION = 1018169843347882076
ROLE_ID = 1523984977371594772

EXPECTED_MESSAGE = (
    "<@1419031701920940163> - mendu__ - **Zůstává Low Tier 3** - MolePVP\n"
    "\n"
    "**HT3 Fighty:**\n"
    "> prohrál 0-4 <@1018169843347882076>\n"
    "\n"
    "<@&1523984977371594772>"
)


def _fight_ticket(**overrides):
    """Otevřený HT Fight ticket (ticketType=fight) bez zápisu."""
    base = tickets.make_ticket(
        channel_id=2001,
        owner_id="1",
        owner_name="alice",
        ign="mendu__",
        kit="MolePVP",
        target_tier="HT3",
        current_tier="LT3",
        eval_ok=True,
        category_id=555,
        ticket_type=tickets.TICKET_TYPE_FIGHT,
        now=NOW,
    )
    base.update(overrides)
    return base


class TicketTypeTests(unittest.TestCase):
    """Typ ticketu (eval/fight) – základ validace „HT Fight ticket"."""

    def test_default_ticket_is_eval(self):
        t = tickets.make_ticket(
            channel_id=1, owner_id="1", owner_name="a", ign="i", kit="k",
            target_tier="HT3", current_tier="LT3", eval_ok=True,
            category_id=5, now=NOW,
        )
        self.assertEqual(tickets.get_ticket_type(t), tickets.TICKET_TYPE_EVAL)
        self.assertFalse(tickets.is_ht_fight_ticket(t))

    def test_explicit_fight_ticket(self):
        self.assertTrue(tickets.is_ht_fight_ticket(_fight_ticket()))
        self.assertEqual(
            tickets.get_ticket_type(_fight_ticket()), tickets.TICKET_TYPE_FIGHT
        )

    def test_missing_type_treated_as_eval(self):
        old = _fight_ticket()
        del old["ticketType"]
        self.assertFalse(tickets.is_ht_fight_ticket(old))


class ValidateInputTests(unittest.TestCase):
    """Validace skóre / HT tieru / statusu / kitu / konfigurace."""

    def test_score_valid_formats(self):
        for score in ("0-4", "4-1", "1-0", "10-8"):
            ok, msg = topresult.validate_ht_fight_score(score)
            self.assertTrue(ok, score)

    def test_score_rejects_malformed(self):
        # zadání vyjmenovává přesně tyhle neplatné formáty:
        for score in ("abc", "4", "4-", "-4", "4-x", "", "   ", "4:1", "4 - 1"):
            ok, msg = topresult.validate_ht_fight_score(score)
            self.assertFalse(ok, repr(score))

    def test_fight_tier_valid(self):
        for t in topresult.HT_FIGHT_TIERS:
            ok, _ = topresult.validate_ht_fight_tier(t)
            self.assertTrue(ok, t)

    def test_fight_tier_rejects_unknown_and_lt3e(self):
        self.assertFalse(topresult.validate_ht_fight_tier("XYZ")[0])
        self.assertFalse(topresult.validate_ht_fight_tier("LT3E")[0])  # eval ≠ fight tier
        self.assertFalse(topresult.validate_ht_fight_tier("")[0])

    def test_status_valid(self):
        self.assertTrue(topresult.validate_ht_fight_status("Zůstává Low Tier 3")[0])
        self.assertTrue(topresult.validate_ht_fight_status("Povýšen na HT3")[0])

    def test_status_rejects_empty_and_too_long(self):
        self.assertFalse(topresult.validate_ht_fight_status("")[0])
        self.assertFalse(topresult.validate_ht_fight_status("   ")[0])
        self.assertFalse(topresult.validate_ht_fight_status("x" * 65)[0])

    def test_outcome_valid(self):
        self.assertTrue(topresult.validate_ht_fight_outcome("Won")[0])
        self.assertTrue(topresult.validate_ht_fight_outcome("lost")[0])
        self.assertFalse(topresult.validate_ht_fight_outcome("maybe")[0])
        self.assertFalse(topresult.validate_ht_fight_outcome("")[0])

    def test_is_registered_kit(self):
        self.assertTrue(topresult.is_registered_kit("MolePVP", ["MolePVP", "AnchorPvP"]))
        self.assertTrue(topresult.is_registered_kit("molepvp", ["MolePVP"]))
        self.assertFalse(topresult.is_registered_kit("Foo", ["MolePVP"]))
        self.assertFalse(topresult.is_registered_kit("", ["MolePVP"]))

    def test_validate_topresult_config(self):
        self.assertTrue(topresult.validate_topresult_config(111, 222)[0])
        self.assertFalse(topresult.validate_topresult_config(0, 0)[0])
        self.assertFalse(topresult.validate_topresult_config(111, 0)[0])
        self.assertFalse(topresult.validate_topresult_config(0, 222)[0])
        self.assertFalse(topresult.validate_topresult_config(None, None)[0])

    def test_config_has_topresult_keys(self):
        # /topresult má vlastní konfiguraci; nesmí mlčky spadnout na běžné
        # výsledkové kanály (/result).
        if config is None:
            self.skipTest("config nelze načíst")
        self.assertTrue(hasattr(config, "TOP_RESULT_CHANNEL_ID"))
        self.assertTrue(hasattr(config, "TOP_RESULT_ROLE_ID"))
        self.assertIsInstance(config.TOP_RESULT_CHANNEL_ID, int)
        self.assertIsInstance(config.TOP_RESULT_ROLE_ID, int)

    def test_ht_fight_outcome_display(self):
        self.assertEqual(topresult.ht_fight_outcome_display("Won"), "vyhrál")
        self.assertEqual(topresult.ht_fight_outcome_display("Lost"), "prohrál")
        self.assertEqual(topresult.ht_fight_outcome_display("won"), "vyhrál")


class FormatMessageTests(unittest.TestCase):
    """Formát zprávy – přesně zachovává styl serveru (sekce 4 zadání)."""

    def test_exact_server_format(self):
        msg = topresult.format_topresult_message(
            player_id=PLAYER_MENTION,
            ign="mendu__",
            tier_status="Zůstává Low Tier 3",
            kit="MolePVP",
            fight_tier="HT3",
            outcome="Lost",
            score="0-4",
            opponent_id=OPPONENT_MENTION,
            role_id=ROLE_ID,
        )
        self.assertEqual(msg, EXPECTED_MESSAGE)

    def test_role_mention_is_real_mention(self):
        # role mention MUSÍ být <@&ROLE_ID> (Discord ping), ne jen jméno -
        # a role jde z nakonfigurovaného ID, žádný uživatelský vstup.
        msg = topresult.format_topresult_message(
            player_id=1, ign="x", tier_status="S",
            kit="MolePVP", fight_tier="HT3", outcome="Won",
            score="4-1", opponent_id=2, role_id=ROLE_ID,
        )
        self.assertIn(f"<@&{ROLE_ID}>", msg)
        self.assertIn("<@&1523984977371594772>", msg)

    def test_won_display_and_tier_normalization(self):
        msg = topresult.format_topresult_message(
            player_id=1, ign="x", tier_status="Povýšen",
            kit="AnchorPvP", fight_tier="ht3", outcome="Won",
            score="4-1", opponent_id=2, role_id=3,
        )
        self.assertIn("**HT3 Fighty:**", msg)  # normalizace malých písmen
        self.assertIn("> vyhrál 4-1 <@2>", msg)
        # role mention je na samostatném řádku MIMO blockquote
        self.assertIn("\n\n<@&3>", msg)


class RecordHTFightFreeTests(unittest.TestCase):
    """Volný HT Fight výsledek (mimo ticket)."""

    FILES = (
        (results.HT_RESULTS_FILE, {}),
        ("players.json", [{"username": "mendu__", "modes": {"MolePVP": "LT3"}, "history": {}}]),
        ("cooldowns.json", {}),
        (tickets.HT_TICKETS_FILE, {}),
    )

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in self.FILES:
            storage.save_data(name, default)

    async def _record(self, **overrides):
        kwargs = dict(
            player_id="1",
            player_name="mendu__",
            ign="mendu__",
            evaluator_id="9",
            evaluator_name="tester",
            kit="MolePVP",
            fight_tier="HT3",
            score="0-4",
            outcome="Lost",
            opponent_id="2",
            opponent_name="souper",
            tier_status="Zůstává Low Tier 3",
            now=NOW,
            date="23.09.2026",
        )
        kwargs.update(overrides)
        return await topresult.record_ht_fight(**kwargs)

    def test_valid_free_result(self):
        async def main():
            rec = await self._record()
            self.assertEqual(rec["result"], "created")
            data = rec["record"]
            self.assertEqual(data["resultType"], "ht_fight")
            self.assertEqual(data["kind"], "ht_fight")
            self.assertIsNone(data["ticketId"])
            self.assertEqual(data["playerId"], "1")
            self.assertEqual(data["ign"], "mendu__")
            self.assertEqual(data["evaluatorId"], "9")
            self.assertEqual(data["kit"], "MolePVP")
            self.assertEqual(data["fightTier"], "HT3")
            self.assertEqual(data["tierStatus"], "Zůstává Low Tier 3")
            self.assertEqual(data["score"], "0-4")
            self.assertEqual(data["outcome"], "Lost")
            self.assertEqual(data["opponentId"], "2")
            self.assertEqual(data["opponentName"], "souper")
            # tier hráče v čase zápasu – jen kontext, žádná změna
            self.assertEqual(data["previousTier"], "LT3")
            # žádná změna tieru = nový tier prázdný
            self.assertEqual(data["newTier"], "")
            # klíč záznamu (idempotence / identifikace)
            self.assertTrue(str(rec["record"]["id"]).startswith(topresult.HT_FIGHT_RESULT_PREFIX))
        asyncio.run(main())

    def test_no_tier_change_and_no_side_effects(self):
        async def main():
            before_players = storage.load_data("players.json", [])
            rec = await self._record()
            self.assertEqual(rec["result"], "created")
            # players.json se NEDOTÝKÁ (žádná změna tieru, žádná historie hráče)
            self.assertEqual(storage.load_data("players.json", []), before_players)
            # cooldowny se NENASTAVUJÍ
            self.assertEqual(storage.load_data("cooldowns.json", {}), {})
            # historie má přesně 1 záznam (resultType=ht_fight)
            hist = storage.load_data(results.HT_RESULTS_FILE, {})
            self.assertEqual(len(hist), 1)
            self.assertEqual(list(hist.values())[0]["resultType"], "ht_fight")
        asyncio.run(main())

    def test_invalid_player_argument(self):
        async def main():
            rec = await self._record(player_id="")
            self.assertEqual(rec["result"], "invalid_argument")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_invalid_kit_argument(self):
        async def main():
            rec = await self._record(kit="")
            self.assertEqual(rec["result"], "invalid_argument")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_invalid_fight_tier(self):
        async def main():
            rec = await self._record(fight_tier="LT3E")
            self.assertEqual(rec["result"], "invalid_tier")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_invalid_score(self):
        async def main():
            for bad in ("abc", "4", "4-", "-4", "4-x"):
                rec = await self._record(score=bad)
                self.assertEqual(rec["result"], "invalid_score", bad)
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_invalid_status(self):
        async def main():
            rec = await self._record(tier_status="   ")
            self.assertEqual(rec["result"], "invalid_status")
        asyncio.run(main())

    def test_restart_safe_history(self):
        async def record():
            return await self._record()
        asyncio.run(record())

        async def read_again():
            rec = await self._record(
                player_id="2", player_name="bob", ign="BobMC",
                opponent_id="1", score="4-1", outcome="Won",
                tier_status="Povýšen na HT3", now=NOW + 1,
                date="24.09.2026",
            )
            self.assertEqual(rec["result"], "created")
            fights = await topresult.get_ht_fight_results()
            self.assertEqual(len(fights), 2)
        asyncio.run(read_again())

    def test_ht_fight_results_filter(self):
        async def main():
            await self._record()
            fights = await topresult.get_ht_fight_results()
            self.assertEqual(len(fights), 1)
            self.assertEqual(fights[0]["resultType"], "ht_fight")
        asyncio.run(main())


class RecordHTFightTicketTests(unittest.TestCase):
    """HT Fight výsledek uvnitř HT Fight ticketu."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            (results.HT_RESULTS_FILE, {}),
            ("players.json", [{"username": "mendu__", "modes": {"MolePVP": "LT3"}, "history": {}}]),
            ("cooldowns.json", {}),
            (tickets.HT_TICKETS_FILE, {"2001": _fight_ticket()}),
        ):
            storage.save_data(name, default)

    async def _record(self, **overrides):
        kwargs = dict(
            ticket_id=2001,
            player_id="1",
            player_name="mendu__",
            ign="mendu__",
            evaluator_id="9",
            evaluator_name="tester",
            kit="MolePVP",
            fight_tier="HT3",
            score="0-4",
            outcome="Lost",
            opponent_id="2",
            opponent_name="souper",
            tier_status="Zůstává Low Tier 3",
            now=NOW,
            date="23.09.2026",
        )
        kwargs.update(overrides)
        return await topresult.record_ht_fight(**kwargs)

    def test_created_inside_fight_ticket(self):
        async def main():
            rec = await self._record()
            self.assertEqual(rec["result"], "created")
            data = rec["record"]
            self.assertEqual(data["ticketId"], "2001")
            self.assertEqual(data["id"], "2001:ht_fight")
            self.assertEqual(data["resultType"], "ht_fight")
            # ticket zůstává OTEVŘENÝ (zavírá ho až /result logika)
            ticket = storage.load_data(tickets.HT_TICKETS_FILE, {})["2001"]
            self.assertEqual(ticket["status"], "open")
            # players.json beze změny
            players = storage.load_data("players.json", [])
            self.assertEqual(players[0]["modes"]["MolePVP"], "LT3")
        asyncio.run(main())

    def test_duplicate_submission_blocked(self):
        async def main():
            first = await self._record()
            self.assertEqual(first["result"], "created")
            second = await self._record(now=NOW + 1000)
            self.assertEqual(second["result"], "duplicate")
            self.assertEqual(second["existing"]["id"], "2001:ht_fight")
            # nic se nepošle dvakrát – historie má jediný záznam
            self.assertEqual(len(storage.load_data(results.HT_RESULTS_FILE, {})), 1)
            existing = await topresult.get_ht_fight_result_for_ticket(2001)
            self.assertIsNotNone(existing)
            self.assertEqual(existing["score"], "0-4")
        asyncio.run(main())

    def test_not_found(self):
        async def main():
            rec = await self._record(ticket_id=9999)
            self.assertEqual(rec["result"], "not_found")
        asyncio.run(main())

    def test_not_fight_ticket_rejected(self):
        async def main():
            eval_ticket = tickets.make_ticket(
                channel_id=3001, owner_id="1", owner_name="a", ign="mendu__",
                kit="MolePVP", target_tier="HT3", current_tier="LT3", eval_ok=True,
                category_id=5, ticket_type=tickets.TICKET_TYPE_EVAL, now=NOW,
            )
            tickets_db = storage.load_data(tickets.HT_TICKETS_FILE, {})
            tickets_db["3001"] = eval_ticket
            storage.save_data(tickets.HT_TICKETS_FILE, tickets_db)
            rec = await self._record(ticket_id=3001)
            self.assertEqual(rec["result"], "not_fight_ticket")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_closed_ticket_rejected(self):
        async def main():
            tickets_db = storage.load_data(tickets.HT_TICKETS_FILE, {})
            tickets_db["2001"]["status"] = "closed"
            storage.save_data(tickets.HT_TICKETS_FILE, tickets_db)
            rec = await self._record()
            self.assertEqual(rec["result"], "ticket_closed")
        asyncio.run(main())

    def test_wrong_player_rejected(self):
        async def main():
            rec = await self._record(player_id="9")  # ne vlastník ticketu
            self.assertEqual(rec["result"], "wrong_player")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())

    def test_wrong_kit_rejected(self):
        async def main():
            rec = await self._record(kit="AnchorPvP")  # ticket je na MolePVP
            self.assertEqual(rec["result"], "wrong_kit")
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE, {}), {})
        asyncio.run(main())


class ResultTypeIntegrationTests(unittest.TestCase):
    """result_type = normal | ht_fight ve stejné kanonické historii."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, default in (
            (results.HT_RESULTS_FILE, {}),
            ("players.json", [{"username": "mendu__", "modes": {"MolePVP": "LT3"}, "history": {"MolePVP": []}}]),
            ("cooldowns.json", {}),
            (tickets.HT_TICKETS_FILE, {"2001": _fight_ticket()}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
        ):
            storage.save_data(name, default)

    def test_make_result_default_type_normal(self):
        rec = results.make_result(
            result_id="x", kind="queue", ticket_id=None, player_id="1",
            player_name="a", ign="i", evaluator_id="9", evaluator_name="t",
            kit="k", previous_tier="N/A", new_tier="LT3", display_tier="LT3",
            score="5-2", outcome="Won", notes=None, eval_flag=False,
            now=NOW, date="23.09.2026",
        )
        self.assertEqual(rec["resultType"], "normal")
        self.assertNotIn("fightTier", rec)
        self.assertEqual(results.get_result_type(rec), "normal")

    def test_get_result_type_fallback_for_old_records(self):
        self.assertEqual(results.get_result_type({"resultType": "ht_fight"}), "ht_fight")
        self.assertEqual(results.get_result_type({"id": "old"}), "normal")
        self.assertEqual(results.get_result_type(None), "normal")

    def test_normal_result_still_works_and_types_coexist(self):
        # /result (result_type=normal) na stejném ticketu musí i nadále
        # fungovat a může koexistovat s HT Fight výsledkem (jiný idempotentní
        # klíč: ticketId vs ticketId:ht_fight).
        async def main():
            # HT Fight výsledek nejdřív (ticket otevřený) – klíč ticketId:ht_fight
            fight = await topresult.record_ht_fight(
                ticket_id=2001, player_id="1", player_name="a",
                ign="mendu__", evaluator_id="9", evaluator_name="t",
                kit="MolePVP", fight_tier="HT3", score="0-4",
                outcome="Lost", opponent_id="2", tier_status="Zůstává LT3",
                now=NOW + 1, date="24.09.2026",
            )
            self.assertEqual(fight["result"], "created")
            self.assertEqual(fight["record"]["id"], "2001:ht_fight")

            # klasický /result (result_type=normal) na stejném ticketu funguje
            # dál a ticket po něm zavře (klíč ticketId).
            normal = await results.record_result(
                ticket_id=2001, player_id="1", player_name="a", ign="mendu__",
                evaluator_id="9", evaluator_name="t", kit="MolePVP",
                new_tier="HT3", display_tier="HT3", score="5-2", outcome="Won",
                now=NOW, date="23.09.2026",
                queue_cooldown_ms=0, ht3_cooldown_ms=0,
            )
            self.assertEqual(normal["result"], "created")
            self.assertEqual(normal["record"]["resultType"], "normal")
            self.assertEqual(normal["record"]["id"], "2001")

            hist = storage.load_data(results.HT_RESULTS_FILE, {})
            self.assertEqual(sorted(hist.keys()), ["2001", "2001:ht_fight"])
            # /result pořád vrací SVŮJ výsledek podle ticketId
            by_ticket = await results.get_result_by_ticket(2001)
            self.assertEqual(by_ticket["resultType"], "normal")
            # HT Fight výsledek má vlastní čtečku
            self.assertIsNotNone(await topresult.get_ht_fight_result_for_ticket(2001))
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
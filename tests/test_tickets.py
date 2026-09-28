"""Testy HT ticket systému services/tickets.py (bez discord.py).

Bývalé JSON-mode testy (``ht_tickets.json`` / ``ht_ticket_logs.json`` /
``ht3_cooldowns.json`` přes ``services.store``, logy do souboru, restart-safe
stav přes soubory) jsou PRYČ spolu s JSON režimem. Produkční chování
``create_ticket`` / ``claim_ticket`` / ``close_ticket`` / ``reopen_ticket`` /
členů / logů nad PostgreSQL je pokryté ``tests/test_services_tickets_db.py``.

Tenhle soubor drží jen čistou logiku nezávislou na úložišti (definice ticketu,
HT3+ žebříček, typ ticketu) + regresní testy, že JSON fallback se do
services/tickets.py nevrátil.
"""

import asyncio
import unittest

from services import tickets

NOW = 1_700_000_000_000
COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000  # 7 dní, jako HT3_COOLDOWN_MS


def _ticket(**overrides):
    """Sestaví kompletní ticket (bez zápisu)."""
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


class TicketShapeTests(unittest.TestCase):
    """make_ticket / get_ticket_type / is_ht_fight_ticket (čisté funkce)."""

    def test_make_ticket_builds_contract_shape(self):
        t = _ticket()
        self.assertEqual(t["id"], "1001")
        self.assertEqual(t["status"], "open")
        self.assertEqual(t["ownerId"], "1")
        self.assertEqual(t["ign"], "AliceMC")
        self.assertEqual(t["targetTier"], "HT3")
        self.assertEqual(t["eval"], True)
        self.assertEqual(t["claimerId"], None)
        self.assertEqual(t["members"], [])
        self.assertEqual(t["ticketType"], tickets.TICKET_TYPE_EVAL)

    def test_fight_ticket_type(self):
        self.assertEqual(tickets.get_ticket_type(_ticket()), "eval")
        self.assertEqual(
            tickets.get_ticket_type(_ticket(ticketType="fight")), "fight"
        )
        self.assertFalse(tickets.is_ht_fight_ticket(_ticket()))
        self.assertTrue(tickets.is_ht_fight_ticket(_ticket(ticketType="fight")))
        # staré záznamy bez pole = eval
        self.assertEqual(tickets.get_ticket_type({"status": "open"}), "eval")
        self.assertFalse(tickets.is_ht_fight_ticket(None))


class TierHelperTests(unittest.TestCase):
    """HT3+ žebříček – čisté pomocné funkce tierů."""

    def test_next_ticket_tier(self):
        self.assertEqual(tickets.next_ticket_tier("LT3"), "HT3")
        self.assertEqual(tickets.next_ticket_tier("LT3E"), "HT3")  # virtuální status
        self.assertEqual(tickets.next_ticket_tier("HT3"), "LT2")
        self.assertEqual(tickets.next_ticket_tier("HT1"), "HT1")  # vrchol
        self.assertIsNone(tickets.next_ticket_tier("RT1"))  # neznámý formát
        self.assertIsNone(tickets.next_ticket_tier(""))

    def test_tier_allows_tickets(self):
        self.assertFalse(tickets.tier_allows_tickets("LT5"))
        self.assertFalse(tickets.tier_allows_tickets("LT3"))
        self.assertTrue(tickets.tier_allows_tickets("LT3E"))
        self.assertTrue(tickets.tier_allows_tickets("HT1"))
        self.assertFalse(tickets.tier_allows_tickets("ABC"))

    def test_effective_ticket_tier(self):
        self.assertEqual(tickets.effective_ticket_tier("LT3", True), "LT3E")
        self.assertEqual(tickets.effective_ticket_tier(None, True), "LT3E")
        # nízké tiery (LT5..HT4 v žebříčku) se s evalem dorovnají na LT3E
        self.assertEqual(tickets.effective_ticket_tier("HT4", True), "LT3E")
        # HT3 a výš zůstávají beze změny
        self.assertEqual(tickets.effective_ticket_tier("HT3", True), "HT3")
        self.assertEqual(tickets.effective_ticket_tier("LT2", True), "LT2")
        self.assertEqual(tickets.effective_ticket_tier("LT3", False), "LT3")


class NoJsonModeTests(unittest.TestCase):
    """services/tickets.py už nemá JSON režim – vyžaduje PostgreSQL."""

    def test_transactional_functions_refuse_explicit_none(self):
        async def main():
            for coro in (
                tickets.create_ticket(
                    channel_id=1001, owner_id="1", owner_name="alice",
                    ign="AliceMC", kit="AnchorPvP", target_tier="HT3",
                    current_tier="LT3", eval_ok=True, category_id=555,
                    now=NOW, session_factory=None,
                ),
                tickets.get_ticket(1001, session_factory=None),
                tickets.find_open_ticket("1", "AnchorPvP", session_factory=None),
                tickets.claim_ticket(1001, "9", "tester", session_factory=None),
                tickets.close_ticket(
                    1001, "9", cooldown_ms=COOLDOWN_MS, now=NOW, session_factory=None
                ),
                tickets.player_tier(
                    "AliceMC", "AnchorPvP", session_factory=None
                ),
            ):
                with self.assertRaises(RuntimeError, msg="PostgreSQL"):
                    await coro
        asyncio.run(main())

    def test_writers_require_session_factory_keyword(self):
        async def main():
            with self.assertRaises(TypeError):
                await tickets.create_ticket(
                    channel_id=1001, owner_id="1", owner_name="alice",
                    ign="AliceMC", kit="AnchorPvP", target_tier="HT3",
                    current_tier="LT3", eval_ok=True, category_id=555, now=NOW,
                )
            with self.assertRaises(TypeError):
                await tickets.get_ticket(1001)
            with self.assertRaises(TypeError):
                await tickets.player_tier("AliceMC", "AnchorPvP")
        asyncio.run(main())

    def test_json_compat_helpers_are_gone(self):
        """Zdrojový regresní test: JSON cesta z services/tickets.py je pryč."""
        import inspect

        src = inspect.getsource(tickets)
        self.assertNotIn("load_data(", src)
        self.assertNotIn("save_data(", src)
        self.assertNotIn("if session_factory is not None:", src)
        self.assertNotIn("HT_TICKETS_FILE", src)
        self.assertNotIn("HT_TICKET_LOGS_FILE", src)
        self.assertNotIn("HT3_COOLDOWNS_FILE", src)
        self.assertNotIn("find_player_tier", src)  # přejmenováno na player_tier (DB)
        self.assertNotIn("from services.store import transaction", src)


if __name__ == "__main__":
    unittest.main()
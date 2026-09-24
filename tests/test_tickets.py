"""Testy HT ticket systému services/tickets.py (bez discord.py).

Pokrývají Phase 2 požadavky:
- automatické vytvoření ticketu,
- prevenci duplicit (i při souběžném vytvoření),
- vlastnictví ticketu,
- Claim / Unclaim,
- /add a /remove členů,
- Close (s HT3+ cooldownem) a Reopen,
- log událostí,
- restart-safe stav (loop-scoped zámky, čtení v novém asyncio.run).
"""

import asyncio
import tempfile
import time
import unittest
from unittest import mock

import storage
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


class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data(tickets.HT_TICKETS_FILE, {})
        storage.save_data(tickets.HT_TICKET_LOGS_FILE, {})
        storage.save_data(tickets.HT3_COOLDOWNS_FILE, {})

    # ------------------------------------------------------------------
    # Vytvoření + prevence duplicit
    # ------------------------------------------------------------------
    def test_create_ticket(self):
        async def main():
            result = await tickets.create_ticket(
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
            self.assertEqual(result["result"], "created")
            t = result["ticket"]
            self.assertEqual(t["id"], "1001")
            self.assertEqual(t["ownerId"], "1")
            self.assertEqual(t["status"], "open")
            self.assertEqual(t["claimerId"], None)
            self.assertEqual(t["members"], [])

            state = storage.load_data(tickets.HT_TICKETS_FILE, {})
            self.assertIn("1001", state)
            self.assertEqual(state["1001"]["kit"], "AnchorPvP")

        asyncio.run(main())

    def test_duplicate_same_owner_and_kit_blocked(self):
        async def main():
            await tickets.create_ticket(
                channel_id=1001, owner_id="1", owner_name="alice",
                ign="AliceMC", kit="AnchorPvP", target_tier="HT3",
                current_tier="LT3", eval_ok=True, category_id=555, now=NOW,
            )
            result = await tickets.create_ticket(
                channel_id=1002, owner_id="1", owner_name="alice",
                ign="AliceMC", kit="anchorpvp", target_tier="HT2",
                current_tier="LT3", eval_ok=True, category_id=555, now=NOW,
            )
            self.assertEqual(result["result"], "duplicate")
            self.assertEqual(result["ticket"]["id"], "1001")
            # duplicitní ticket se nezapsal
            self.assertNotIn("1002", storage.load_data(tickets.HT_TICKETS_FILE, {}))

        asyncio.run(main())

    def test_duplicate_case_insensitive_kit(self):
        async def main():
            await tickets.create_ticket(
                channel_id=1001, owner_id="1", owner_name="alice", ign="A",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            existing = await tickets.find_open_ticket("1", "anchorpvp")
            self.assertIsNotNone(existing)
            self.assertEqual(existing["id"], "1001")

        asyncio.run(main())

    def test_different_kit_or_player_not_duplicate(self):
        async def main():
            await tickets.create_ticket(
                channel_id=1001, owner_id="1", owner_name="alice", ign="A",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            # jiný kit, stejný hráč
            r1 = await tickets.create_ticket(
                channel_id=1002, owner_id="1", owner_name="alice", ign="A",
                kit="MolePVP", target_tier="HT2", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            self.assertEqual(r1["result"], "created")
            # stejný kit, jiný hráč
            r2 = await tickets.create_ticket(
                channel_id=1003, owner_id="2", owner_name="bob", ign="BobMC",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            self.assertEqual(r2["result"], "created")

        asyncio.run(main())

    def test_closed_ticket_does_not_block_new_ticket(self):
        async def main():
            await tickets.create_ticket(
                channel_id=1001, owner_id="1", owner_name="alice", ign="A",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.create_ticket(
                channel_id=1002, owner_id="1", owner_name="alice", ign="A",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            # zavřený ticket nevadí (nový se otevřít může; cooldown hlídá panel)
            self.assertEqual(result["result"], "created")

        asyncio.run(main())

    def test_concurrent_create_no_duplicate_channels(self):
        """Souběžné vytvoření stejného hráče+kitu → jeden ticket, jedna duplicita."""

        async def create(channel_id):
            return await tickets.create_ticket(
                channel_id=channel_id, owner_id="1", owner_name="alice", ign="A",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )

        async def main():
            results = await asyncio.gather(create(1001), create(1002))
            statuses = sorted(r["result"] for r in results)
            self.assertEqual(statuses, ["created", "duplicate"])
            state = storage.load_data(tickets.HT_TICKETS_FILE, {})
            self.assertEqual(len(state), 1)

        asyncio.run(main())

    # ------------------------------------------------------------------
    # Claim / Unclaim
    # ------------------------------------------------------------------
    async def _open_ticket(self, channel_id=1001, owner_id="1"):
        return await tickets.create_ticket(
            channel_id=channel_id, owner_id=owner_id, owner_name="alice", ign="A",
            kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
            eval_ok=True, category_id=555, now=NOW,
        )

    def test_claim_success(self):
        async def main():
            await self._open_ticket()
            result = await tickets.claim_ticket("1001", "9", "tester")
            self.assertEqual(result["result"], "claimed")
            self.assertEqual(result["ticket"]["claimerId"], "9")
            self.assertEqual(result["ticket"]["claimerName"], "tester")

        asyncio.run(main())

    def test_claim_own_ticket_blocked(self):
        async def main():
            await self._open_ticket()
            result = await tickets.claim_ticket("1001", "1", "alice")
            self.assertEqual(result["result"], "own_ticket")
            self.assertIsNone(storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]["claimerId"])

        asyncio.run(main())

    def test_claim_already_claimed(self):
        async def main():
            await self._open_ticket()
            await tickets.claim_ticket("1001", "9", "testerA")
            result = await tickets.claim_ticket("1001", "10", "testerB")
            self.assertEqual(result["result"], "already_claimed")
            self.assertEqual(result["claimer_id"], "9")

        asyncio.run(main())

    def test_claim_same_claimer_idempotent(self):
        async def main():
            await self._open_ticket()
            await tickets.claim_ticket("1001", "9", "tester")
            result = await tickets.claim_ticket("1001", "9", "tester")
            self.assertEqual(result["result"], "claimed")

        asyncio.run(main())

    def test_claim_closed_ticket(self):
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.claim_ticket("1001", "9", "tester")
            self.assertEqual(result["result"], "not_open")

        asyncio.run(main())

    def test_concurrent_claim_only_one_wins(self):
        async def main():
            await self._open_ticket()
            results = await asyncio.gather(
                tickets.claim_ticket("1001", "9", "testerA"),
                tickets.claim_ticket("1001", "10", "testerB"),
            )
            winners = [r for r in results if r["result"] == "claimed"]
            self.assertEqual(len(winners), 1)
            claimed = [r for r in results if r["result"] == "already_claimed"]
            self.assertEqual(len(claimed), 1)

        asyncio.run(main())

    def test_unclaim_success(self):
        async def main():
            await self._open_ticket()
            await tickets.claim_ticket("1001", "9", "tester")
            result = await tickets.unclaim_ticket("1001", "9")
            self.assertEqual(result["result"], "unclaimed")
            self.assertEqual(result["previous"]["claimer_id"], "9")
            self.assertIsNone(storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]["claimerId"])

        asyncio.run(main())

    def test_unclaim_by_non_claimer_requires_force(self):
        async def main():
            await self._open_ticket()
            await tickets.claim_ticket("1001", "9", "tester")
            result = await tickets.unclaim_ticket("1001", "10")
            self.assertEqual(result["result"], "not_claimer")
            # force (tester může uvolnit cizí claim)
            result = await tickets.unclaim_ticket("1001", "10", force=True)
            self.assertEqual(result["result"], "unclaimed")

        asyncio.run(main())

    def test_unclaim_when_not_claimed(self):
        async def main():
            await self._open_ticket()
            result = await tickets.unclaim_ticket("1001", "9")
            self.assertEqual(result["result"], "not_claimed")

        asyncio.run(main())

    # ------------------------------------------------------------------
    # /add a /remove členů
    # ------------------------------------------------------------------
    def test_add_member(self):
        async def main():
            await self._open_ticket()
            result = await tickets.add_member("1001", "5")
            self.assertEqual(result["result"], "added")
            self.assertEqual(storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]["members"], ["5"])
            # druhý přidání = už je členem
            result = await tickets.add_member("1001", "5")
            self.assertEqual(result["result"], "already_member")

        asyncio.run(main())

    def test_add_owner_is_owner(self):
        async def main():
            await self._open_ticket()
            result = await tickets.add_member("1001", "1")
            self.assertEqual(result["result"], "is_owner")

        asyncio.run(main())

    def test_add_to_closed_ticket(self):
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.add_member("1001", "5")
            self.assertEqual(result["result"], "not_open")

        asyncio.run(main())

    def test_remove_member(self):
        async def main():
            await self._open_ticket()
            await tickets.add_member("1001", "5")
            await tickets.add_member("1001", "6")
            result = await tickets.remove_member("1001", "5")
            self.assertEqual(result["result"], "removed")
            self.assertEqual(storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]["members"], ["6"])
            result = await tickets.remove_member("1001", "5")
            self.assertEqual(result["result"], "not_member")

        asyncio.run(main())

    def test_remove_owner_or_claimer_blocked(self):
        async def main():
            await self._open_ticket()
            await tickets.claim_ticket("1001", "9", "tester")
            self.assertEqual((await tickets.remove_member("1001", "1"))["result"], "is_owner")
            self.assertEqual((await tickets.remove_member("1001", "9"))["result"], "is_claimer")

        asyncio.run(main())

    # ------------------------------------------------------------------
    # Close / Reopen + cooldown
    # ------------------------------------------------------------------
    def test_close_sets_cooldown(self):
        async def main():
            await self._open_ticket()
            result = await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            self.assertEqual(result["result"], "closed")
            t = storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]
            self.assertEqual(t["status"], "closed")
            self.assertEqual(t["closedAt"], NOW)

            cds = storage.load_data(tickets.HT3_COOLDOWNS_FILE, {})
            self.assertEqual(cds["1"]["AnchorPvP"], NOW + COOLDOWN_MS)

        asyncio.run(main())

    def test_double_close(self):
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW + 1)
            self.assertEqual(result["result"], "already_closed")

        asyncio.run(main())

    def test_reopen(self):
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.reopen_ticket("1001", "9")
            self.assertEqual(result["result"], "reopened")
            t = storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]
            self.assertEqual(t["status"], "open")
            self.assertIsNone(t["closedAt"])

        asyncio.run(main())

    def test_reopen_when_open(self):
        async def main():
            await self._open_ticket()
            result = await tickets.reopen_ticket("1001", "9")
            self.assertEqual(result["result"], "not_closed")

        asyncio.run(main())

    def test_reopen_blocked_by_active_cooldown(self):
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.reopen_ticket(
                "1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW + 1000
            )
            self.assertEqual(result["result"], "cooldown")
            self.assertEqual(result["kit"], "AnchorPvP")
            self.assertEqual(result["remaining_ms"], COOLDOWN_MS - 1000)
            # ticket zůstává zavřený
            t = storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]
            self.assertEqual(t["status"], "closed")
            self.assertEqual(t["closedAt"], NOW)

        asyncio.run(main())

    def test_reopen_allowed_after_cooldown_expires(self):
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.reopen_ticket(
                "1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW + COOLDOWN_MS + 1
            )
            self.assertEqual(result["result"], "reopened")
            t = storage.load_data(tickets.HT_TICKETS_FILE, {})["1001"]
            self.assertEqual(t["status"], "open")

        asyncio.run(main())

    def test_reopen_without_cooldown_preserves_legacy_behavior(self):
        # default cooldown_ms=0 = původní chování, žádná cooldown kontrola
        async def main():
            await self._open_ticket()
            await tickets.close_ticket("1001", "9", cooldown_ms=COOLDOWN_MS, now=NOW)
            result = await tickets.reopen_ticket("1001", "9")
            self.assertEqual(result["result"], "reopened")

        asyncio.run(main())

    def test_ticket_not_found_operations(self):
        async def main():
            self.assertEqual((await tickets.claim_ticket("9999", "9", "t"))["result"], "not_found")
            self.assertEqual((await tickets.unclaim_ticket("9999", "9"))["result"], "not_found")
            self.assertEqual((await tickets.add_member("9999", "5"))["result"], "not_found")
            self.assertEqual((await tickets.remove_member("9999", "5"))["result"], "not_found")
            self.assertEqual((await tickets.close_ticket("9999", "9", cooldown_ms=COOLDOWN_MS, now=NOW))["result"], "not_found")
            self.assertEqual((await tickets.reopen_ticket("9999", "9"))["result"], "not_found")

        asyncio.run(main())

    # ------------------------------------------------------------------
    # Log událostí
    # ------------------------------------------------------------------
    def test_event_log(self):
        async def main():
            await self._open_ticket()
            await tickets.log_ticket_event("1001", "created", "1", "alice", details="vytvořeno", now=NOW)
            await tickets.log_ticket_event("1001", "claimed", "9", "tester", now=NOW + 1)

            logs = await tickets.get_ticket_logs("1001")
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0]["action"], "created")
            self.assertEqual(logs[0]["actorId"], "1")
            self.assertEqual(logs[1]["action"], "claimed")
            self.assertEqual(logs[1]["details"], None)

            # logy jiného ticketu jsou oddělené
            self.assertEqual(await tickets.get_ticket_logs("7777"), [])

        asyncio.run(main())

    # ------------------------------------------------------------------
    # Restart-safe stav
    # ------------------------------------------------------------------
    def test_state_survives_separate_event_loops(self):
        """Zápis i čtení v oddělených asyncio.run() cyklech (loop-scoped zámky)."""

        async def write():
            await tickets.create_ticket(
                channel_id=1001, owner_id="1", owner_name="alice", ign="A",
                kit="AnchorPvP", target_tier="HT3", current_tier="LT3",
                eval_ok=True, category_id=555, now=NOW,
            )
            await tickets.claim_ticket("1001", "9", "tester")

        async def read():
            t = await tickets.get_ticket("1001")
            self.assertIsNotNone(t)
            self.assertEqual(t["status"], "open")
            self.assertEqual(t["claimerId"], "9")
            self.assertEqual(t["targetTier"], "HT3")

        asyncio.run(write())
        asyncio.run(read())  # nový event loop

    # ------------------------------------------------------------------
    # Tier pomocné funkce
    # ------------------------------------------------------------------
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

    def test_find_player_tier(self):
        storage.save_data(
            "players.json",
            [
                {"username": "aliceMC", "modes": {"AnchorPvP": "LT3", "MolePVP": "RT1"}},
                {"username": "BOBMC", "modes": {"AnchorPvP": "HT2"}},
            ],
        )
        self.assertEqual(tickets.find_player_tier("aliceMC", "AnchorPvP"), "LT3")
        self.assertEqual(tickets.find_player_tier("BOBMC", "AnchorPvP"), "HT2")
        # hráč bez záznamu
        self.assertIsNone(tickets.find_player_tier("nobody", "AnchorPvP"))
        # RT tier (R-tiery) se vrací taky – jen tickety se nekontrolují
        self.assertEqual(tickets.find_player_tier("AliceMC", "MolePVP"), "RT1")
        # poškozená data nespadnou
        storage.save_data("players.json", "not-a-list")
        self.assertIsNone(tickets.find_player_tier("aliceMC", "AnchorPvP"))

    def test_find_player_tier_prefers_discord_id(self):
        storage.save_data(
            "players.json",
            [
                {"username": "AliceMC", "discordId": "111", "modes": {"AnchorPvP": "LT3"}},
                {"username": "BOBMC", "discordId": "222", "modes": {"AnchorPvP": "HT2"}},
                {"username": "unowned", "modes": {"AnchorPvP": "LT2"}},
            ],
        )
        # Discord ID má přednost: IGN "AliceMC" s cizím ID = tier VLASTNÍKA ID
        self.assertEqual(
            tickets.find_player_tier("AliceMC", "AnchorPvP", discord_id="222"), "HT2"
        )
        # neexistující IGN + existující Discord ID stále funguje
        self.assertEqual(
            tickets.find_player_tier("X", "AnchorPvP", discord_id="222"), "HT2"
        )
        self.assertIsNone(tickets.find_player_tier("X", "AnchorPvP", discord_id="999"))
        # bez Discord ID → klasická case-insensitive IGN shoda (legacy)
        self.assertEqual(tickets.find_player_tier("AliceMC", "AnchorPvP"), "LT3")
        self.assertEqual(tickets.find_player_tier("unowned", "AnchorPvP"), "LT2")


if __name__ == "__main__":
    unittest.main()
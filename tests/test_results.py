"""Testy „unified HT result system" – services/results.py (bez discord.py).

Bývalé JSON-mode testy (``ht_results.json`` / ``players.json`` přes
``services.store``, ``apply_result_to_players`` povýšení do players.json,
``canonical_kit_name``/``migrate_mode_keys``) jsou PRYČ spolu s JSON režimem.
Produkční chování ``record_result`` nad PostgreSQL (queue/ticket výsledky,
cooldown, idempotence, zavření ticketu + event log, sběh a konzistence při
souběžných zápisech) je pokryté ``tests/test_services_results_db.py``.

Tenhle soubor drží jen čistou logiku nezávislou na úložišti (validace tieru)
a cog-plumbing testy (self-result gate, předání grantu kanonické službě
``commit_confirmed_promotion`` s přesným payloadem) + jeden regresní test, že
JSON fallback se do services/results.py nevrátil.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs.results import Results
from cogs.roles import TierRoleGrant
from services import results, tickets

NOW = 1_700_000_000_000
QUEUE_COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000  # 4 dny, jako PLAYER_COOLDOWN_MS
HT3_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000  # 7 dní, jako HT3_COOLDOWN_MS

# Sentinel: existuje, ale nikdy se nevolá jako hlavní čtení (všechny
# DB-volající cesty jsou v cog-plumbing testech zamockované).
_FAKE_SESSION_FACTORY = object()

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


class ResultCogSelfResultTests(unittest.TestCase):
    """item 6: tester si NEMŮŽE zapsat výsledek sám sobě (admin ano)."""

    def setUp(self):
        self._claims = []

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
        # G0 audit fix: result() now reads self.bot.db_session_factory
        # earlier — a real cog always has .bot via __init__; this stub
        # needs it explicitly.
        cog.bot = SimpleNamespace(db_session_factory=None)
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
        cog.bot = SimpleNamespace(db_session_factory=None)
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
        cog.bot = SimpleNamespace(db_session_factory=None)
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


class ResultDbMirrorTests(unittest.TestCase):
    """item 13/5d: cog předá celý grant kanonické službě
    ``commit_confirmed_promotion`` s EXAKTNÍM payloadem – chytilo by
    int('LT2'). POZOR: gate už NENÍ v cogu. Cog grant pouze předá
    (včetně nepotvrzeného); rozhodnutí „zapsat do PG nebo ne" je jediná
    odpovědnost služby, která navíc vyžaduje `verified` (G0/invariant 6)."""

    def _interaction(self, user_id):
        inter = mock.MagicMock()
        inter.user = SimpleNamespace(
            id=user_id,
            name="tester",
            display_name="tester",
            roles=[SimpleNamespace(id=777, name="Tester")],
            guild_permissions=SimpleNamespace(administrator=True),
        )
        inter.guild = mock.MagicMock()
        inter.guild.get_member.return_value = SimpleNamespace(id=999, voice=None)
        inter.channel = mock.MagicMock()  # mimo HT ticket → queue cesta
        inter.response.send_message = mock.AsyncMock()
        inter.response.defer = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        return inter

    def _call(self, cog, inter, *, grant_result):
        cog.bot.get_channel.return_value.send = mock.AsyncMock()
        cm = __import__("cogs.results", fromlist=["Results"])
        with mock.patch.object(cm, "has_tester_role", return_value=True), \
             mock.patch.object(cm, "validate_result_tier", return_value=(True, "")), \
             mock.patch.object(cm, "get_kits", return_value=["AnchorPvP"]), \
             mock.patch.object(
                 cm, "record_result",
                 new=mock.AsyncMock(return_value={
                     "result": "created",
                     "previous_tier": "LT3",
                     "record": {"id": "rec-abc", "previousTier": "LT3", "newTier": "LT3"},
                 }),
             ), \
             mock.patch.object(cm, "get_result_channel_id", return_value=123), \
             mock.patch.object(cm, "today_cz", return_value="01.01.2026"), \
             mock.patch.object(
                 cm, "auto_grant_kit_role", new=mock.AsyncMock(return_value=grant_result)
             ), \
             mock.patch.object(
                 cm, "leave_queue", new=mock.AsyncMock(return_value=False)
             ), \
             mock.patch.object(
                 cm, "update_panel", new=mock.AsyncMock(return_value=None)
             ), \
             mock.patch.object(
                 cm, "remove_pulled_player", new=mock.AsyncMock(return_value=False)
             ), \
             mock.patch(
                 "db.services.commit_confirmed_promotion",
                 new=mock.AsyncMock(return_value=SimpleNamespace(message="ok")),
             ) as commit_mock:
            hrac = SimpleNamespace(id=999, name="BobMC", display_name="BobMC")
            asyncio.run(cog.result.callback(
                cog,
                interaction=inter,
                hrac=hrac,
                ign="AliceMC",
                kit="AnchorPvP",
                tier="LT3",
                score="3:1",
                outcome="WON",
            ))
            return commit_mock

    def test_queue_promotion_commits_exact_db_kwargs(self):
        cog = Results.__new__(Results)
        cog.bot = mock.MagicMock()
        cog.bot.db_session_factory = _FAKE_SESSION_FACTORY
        inter = self._interaction(user_id=777)
        grant = TierRoleGrant(
            ok=True, verified=True, tier_role_id=202, note="ok"
        )
        commit_mock = self._call(cog, inter, grant_result=grant)
        commit_mock.assert_awaited_once()
        args, kw = commit_mock.await_args
        self.assertIs(args[0], cog.bot.db_session_factory)
        self.assertIs(kw["grant"], grant)
        self.assertEqual(kw["result_key"], "result:rec-abc")
        self.assertEqual(kw["kind"], "queue")
        self.assertEqual(kw["discord_id"], 999)
        self.assertEqual(kw["ign"], "AliceMC")
        self.assertEqual(kw["kit_key"], "anchorpvp")
        self.assertEqual(kw["new_tier_code"], "LT3")
        self.assertNotIn("discord_role_id", kw)  # služba ho vezme z grantu
        self.assertEqual(kw["previous_tier_code"], "LT3")
        self.assertEqual(kw["score"], "3:1")
        self.assertEqual(kw["outcome"], "WON")
        self.assertEqual(kw["evaluator_discord_id"], 777)
        self.assertIsNone(kw["ticket_channel_id"])
        self.assertIsNone(kw["notes"])
        self.assertFalse(kw["eval_flag"])
        self.assertEqual(kw["date"], "01.01.2026")
        self.assertIsNone(kw["close_ticket_channel_id"])
        self.assertEqual(kw["audit_actor_id"], 777)
        self.assertEqual(kw["audit_actor_name"], str(inter.user))

    def test_grant_failed_is_handed_to_the_canonical_service(self):
        """Cog už NEMÁ vlastní gate – nepotvrzený grant se předá službě, která
        ho odmítne. Tím se odstraní duplicitní (a snadno rozdvojená) podmínka
        mezi cogs/results.py a cogs/topresult.py."""
        cog = Results.__new__(Results)
        cog.bot = mock.MagicMock()
        cog.bot.db_session_factory = _FAKE_SESSION_FACTORY
        failed = TierRoleGrant(ok=False, note="nelze", tier_role_id=None)
        commit_mock = self._call(
            cog, self._interaction(user_id=777), grant_result=failed
        )
        commit_mock.assert_awaited_once()
        self.assertIs(commit_mock.await_args.kwargs["grant"], failed)

    def test_legacy_empty_grant_is_handed_to_the_canonical_service(self):
        cog = Results.__new__(Results)
        cog.bot = mock.MagicMock()
        cog.bot.db_session_factory = _FAKE_SESSION_FACTORY
        commit_mock = self._call(
            cog, self._interaction(user_id=777), grant_result=""
        )
        commit_mock.assert_awaited_once()
        self.assertEqual(commit_mock.await_args.kwargs["grant"], "")

    def test_unverified_grant_never_reaches_postgres(self):
        """End-to-end (bez mocku služby): `ok=True` BEZ `verified` musí být
        odmítnuto a do PG se nesmí zapsat nic."""
        from db.services.promotion import commit_confirmed_promotion

        async def run():
            return await commit_confirmed_promotion(
                _FAKE_SESSION_FACTORY,
                grant=TierRoleGrant(ok=True, tier_role_id=202, note="ok"),
                result_key="result:rec-abc",
                kind="queue",
                discord_id=999,
                ign="AliceMC",
                kit_key="anchorpvp",
                new_tier_code="LT3",
            )

        outcome = asyncio.run(run())
        self.assertFalse(outcome.committed)
        self.assertFalse(outcome.wedged)


class NoJsonModeTests(unittest.TestCase):
    """services/results.py už NEMÁ JSON režim – vyžaduje PostgreSQL."""

    def test_record_result_requires_session_factory(self):
        async def main():
            with self.assertRaises(TypeError):
                await results.record_result(
                    player_id="1", player_name="alice", ign="AliceMC",
                    evaluator_id="9", evaluator_name="tester", kit="AnchorPvP",
                    new_tier="LT3", display_tier="LT3", score="3:1",
                    outcome="Won", notes=None, eval_flag=False, now=NOW,
                    date="23.09.2026",
                    queue_cooldown_ms=QUEUE_COOLDOWN_MS,
                    ht3_cooldown_ms=0,
                )
        asyncio.run(main())

    def test_readers_refuse_explicit_none(self):
        async def main():
            for coro in (
                results.get_result_by_ticket(1001, session_factory=None),
                results.get_results_for_player("1", session_factory=None),
                results.get_all_results(session_factory=None),
            ):
                with self.assertRaises(RuntimeError, msg="PostgreSQL"):
                    await coro
        asyncio.run(main())

    def test_json_compat_helpers_are_gone(self):
        """Zdrojový regresní test: JSON cesta z services/results.py je pryč."""
        import inspect

        src = inspect.getsource(results)
        self.assertNotIn("load_data(", src)
        self.assertNotIn("save_data(", src)
        self.assertNotIn("if session_factory is not None:", src)
        self.assertNotIn("def apply_result_to_players", src)
        self.assertNotIn("def canonical_kit_name", src)
        self.assertNotIn("def migrate_mode_keys", src)
        self.assertNotIn("from services.store import transaction", src)
        self.assertNotIn("HT_RESULTS_FILE", src)


if __name__ == "__main__":
    unittest.main()
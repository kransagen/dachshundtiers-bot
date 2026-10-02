"""Testy HT Fight výsledků – /topresult (services/topresult.py, bez discord.py).

Bývalé JSON-mode testy (``ht_results.json`` / ``players.json`` přes
``services.store``, ``apply_result_to_players`` povýšení atd.) jsou PRYČ spolu
s JSON režimem. Produkční chování ``record_ht_fight`` / ``set_ht_fight_announcement``
/ readerů nad PostgreSQL je pokryté ``tests/test_services_results_db.py``
(výhra/prohra, cooldown, ticket lifecycle, bridge, announcement, readers).

Tenhle soubor drží jen čistou logiku nezávislou na úložišti (validace
vstupů, formát zprávy, typ ticketu, řádek postupu) a cog-plumbing testy
(bridge parametr, commit kwargs, failure matrix) + jeden regresní test, že
JSON fallback se do services/topresult.py nevrátil.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import storage  # noqa: F401  (imported for legacy-data smoke; unused now)
from cogs.roles import TierRoleGrant
from services import tickets, topresult

try:
    import config
except Exception:  # noqa: BLE001  (config se čte i bez dotenv)
    config = None

NOW = 1_700_000_000_000

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
        channel_id=2001, owner_id="1", owner_name="alice", ign="mendu__",
        kit="MolePVP", target_tier="HT3", current_tier="LT3", eval_ok=True,
        category_id=555, ticket_type=tickets.TICKET_TYPE_FIGHT, now=NOW,
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

    def test_bridge_valid_tiers(self):
        for t in topresult.HT_FIGHT_TIERS:
            ok, _ = topresult.validate_ht_fight_bridge(t)
            self.assertTrue(ok, t)

    def test_bridge_rejects_unknown_and_lt3e(self):
        self.assertFalse(topresult.validate_ht_fight_bridge("XYZ")[0])
        self.assertFalse(topresult.validate_ht_fight_bridge("LT3E")[0])  # eval ≠ reálný tier
        self.assertFalse(topresult.validate_ht_fight_bridge("")[0])
        self.assertFalse(topresult.validate_ht_fight_bridge("   ")[0])



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



class PromotionLineFormatTests(unittest.TestCase):
    """Řádek postupu ve zprávě – jen when player skutečně postoupil."""

    def test_promotion_line_added_when_new_different(self):
        msg = topresult.format_topresult_message(
            player_id=1, ign="x", tier_status="Povýšen na HT3",
            kit="MolePVP", fight_tier="HT3", outcome="Won", score="4-1",
            opponent_id=2, previous_tier="LT3", new_tier="HT3", role_id=ROLE_ID,
        )
        self.assertIn("**Postup: LT3 → HT3**", msg)

    def test_no_promotion_line_when_new_equals_previous(self):
        msg = topresult.format_topresult_message(
            player_id=1, ign="x", tier_status="Povýšen na HT3",
            kit="MolePVP", fight_tier="HT3", outcome="Won", score="4-1",
            opponent_id=2, previous_tier="HT1", new_tier="HT1", role_id=ROLE_ID,
        )
        self.assertNotIn("Postup", msg)

    def test_no_promotion_line_for_loss(self):
        msg = topresult.format_topresult_message(
            player_id=1, ign="x", tier_status="Zůstává Low Tier 3",
            kit="MolePVP", fight_tier="HT3", outcome="Lost", score="0-4",
            opponent_id=2, previous_tier="LT3", new_tier="", role_id=ROLE_ID,
        )
        self.assertNotIn("Postup", msg)



class TopResultCogBridgeTests(unittest.TestCase):
    """Plumbing bridge parametru v /topresult (cog, bez sítě)."""

    CHANNEL_ID = 5555
    ROLE_ID = 6666

    def setUp(self):
        self.cog_module = __import__("cogs.topresult", fromlist=["TopResult"])
        cm = self.cog_module
        self._patches = [
            mock.patch.object(cm, "TOP_RESULT_CHANNEL_ID", self.CHANNEL_ID),
            mock.patch.object(cm, "TOP_RESULT_ROLE_ID", self.ROLE_ID),
            mock.patch.object(cm, "has_tester_role", return_value=True),
            mock.patch.object(cm, "validate_topresult_config", return_value=(True, "")),
            mock.patch.object(cm, "is_registered_kit", return_value=True),
            mock.patch.object(cm, "validate_ht_fight_tier", return_value=(True, "")),
            mock.patch.object(cm, "validate_ht_fight_score", return_value=(True, "")),
            mock.patch.object(cm, "validate_ht_fight_status", return_value=(True, "")),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _call(self, record_result, *, bridge="LT2"):
        cm = self.cog_module
        with mock.patch.object(
            cm,
            "get_kits",
            new=mock.AsyncMock(return_value=["MolePVP"]),
        ), mock.patch.object(
            cm, "record_ht_fight", new=mock.AsyncMock(return_value=record_result)
        ) as rec_mock, mock.patch.object(
            cm, "set_ht_fight_announcement", new=mock.AsyncMock(return_value={"result": "ok"})
        ), mock.patch.object(
            cm,
            "auto_grant_kit_role",
            new=mock.AsyncMock(
                return_value=TierRoleGrant(
                    ok=True, verified=True, tier_role_id=202, note="ok"
                )
            ),
        ) as grant_mock, mock.patch.object(
            cm, "get_ticket", new=mock.AsyncMock(return_value=None)
        ):
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=111))
            bot = mock.MagicMock()
            bot.get_channel.return_value = channel
            inter = mock.MagicMock()
            inter.response.defer = mock.AsyncMock()
            inter.followup.send = mock.AsyncMock()
            inter.user.name = "tester"
            inter.guild.get_role.return_value = mock.MagicMock(id=self.ROLE_ID)
            inter.channel_id = 9999

            cog = cm.TopResult(bot)
            hrac = mock.MagicMock(id=1, display_name="mendu__", name="mendu__")
            opp = mock.MagicMock(id=2, display_name="souper", name="souper")
            asyncio.run(cog.topresult.callback(
                cog,
                interaction=inter,
                fight_tier="HT3",
                outcome="Won",
                score="4-1",
                opponent=opp,
                tier_status="Povýšen na LT2",
                hrac=hrac,
                ign="mendu__",
                kit="MolePVP",
                bridge=bridge,
            ))
            return inter, channel, rec_mock, grant_mock

    def test_bridge_param_is_passed_and_promotes(self):
        record_result = {
            "result": "created",
            "record": {
                "id": "x",
                "previousTier": "LT3",
                "newTier": "LT2",
                "bridgeTier": "LT2",
            },
            "previous_tier": "LT3",
        }
        inter, channel, rec_mock, grant_mock = self._call(record_result)

        # bridge jde přes službu (a neztratí se v plumbing)
        self.assertEqual(rec_mock.await_args.kwargs["bridge"], "LT2")
        # role se udělí pro cílový (bridge) tier
        self.assertEqual(grant_mock.await_args.kwargs["tier_up"], "LT2")
        # zpráva do TOP_RESULT kanálu obsahuje postup LT3 → LT2
        channel.send.assert_awaited_once()
        content = channel.send.await_args.kwargs["content"]
        self.assertIn("**Postup: LT3 → LT2**", content)
        # potvrzení pro testera zmiňuje bridge
        inter.followup.send.assert_awaited_once()
        reply = inter.followup.send.await_args.args[0]
        self.assertIn("Povýšení: LT3 → LT2", reply)
        self.assertIn("bridge", reply)

    def test_invalid_bridge_shows_clear_message(self):
        record_result = {
            "result": "invalid_bridge",
            "message": "❌ Bridge tier `LT1` není vyšší než aktuální tier hráče `LT3`.",
        }
        inter, channel, _, _ = self._call(record_result, bridge="LT1")

        inter.followup.send.assert_awaited_once()
        msg = inter.followup.send.await_args.args[0]
        self.assertIn("Bridge tier", msg)
        self.assertIn("LT3", msg)
        # nic se neposlalo do kanálu a žádná role se neudělovala
        channel.send.assert_not_awaited()



class TopResultDbMirrorTests(unittest.TestCase):
    """item 13: 1a2 – cog předá celý grant kanonické službě
    ``commit_confirmed_promotion`` s EXAKTNÍM payloadem (chytilo by chybu
    z rodiny int('LT2')). POZOR: gate už NENÍ v cogu – rozhodnutí „zapsat
    do PG nebo ne" (včetně požadavku na `verified`) má jedině služba."""

    CHANNEL_ID = 5555
    ROLE_ID = 6666

    def setUp(self):
        self.cog_module = __import__("cogs.topresult", fromlist=["TopResult"])
        cm = self.cog_module
        self._patches = [
            mock.patch.object(cm, "TOP_RESULT_CHANNEL_ID", self.CHANNEL_ID),
            mock.patch.object(cm, "TOP_RESULT_ROLE_ID", self.ROLE_ID),
            mock.patch.object(cm, "has_tester_role", return_value=True),
            mock.patch.object(cm, "validate_topresult_config", return_value=(True, "")),
            mock.patch.object(cm, "is_registered_kit", return_value=True),
            mock.patch.object(cm, "validate_ht_fight_tier", return_value=(True, "")),
            mock.patch.object(cm, "validate_ht_fight_score", return_value=(True, "")),
            mock.patch.object(cm, "validate_ht_fight_status", return_value=(True, "")),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _call(self, record_result, *, grant_result):
        cm = self.cog_module
        with mock.patch.object(
            cm, "get_kits", new=mock.AsyncMock(return_value=["MolePVP"])
        ), mock.patch.object(
            cm, "record_ht_fight", new=mock.AsyncMock(return_value=record_result)
        ) as rec_mock, mock.patch.object(
            cm, "set_ht_fight_announcement", new=mock.AsyncMock(return_value={"result": "ok"})
        ), mock.patch.object(
            cm, "auto_grant_kit_role", new=mock.AsyncMock(return_value=grant_result)
        ) as grant_mock, mock.patch.object(
            cm, "get_ticket", new=mock.AsyncMock(return_value=None)
        ), mock.patch(
            "db.services.commit_confirmed_promotion",
            new=mock.AsyncMock(return_value=SimpleNamespace(message="ok")),
        ) as commit_mock:
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=111))
            bot = mock.MagicMock()
            bot.get_channel.return_value = channel
            inter = mock.MagicMock()
            inter.response.defer = mock.AsyncMock()
            inter.followup.send = mock.AsyncMock()
            inter.user.name = "tester"
            inter.guild.get_role.return_value = mock.MagicMock(id=self.ROLE_ID)
            inter.channel_id = 9999

            cog = cm.TopResult(bot)
            hrac = mock.MagicMock(id=1, display_name="mendu__", name="mendu__")
            opp = mock.MagicMock(id=2, display_name="souper", name="souper")
            asyncio.run(cog.topresult.callback(
                cog,
                interaction=inter,
                fight_tier="HT3",
                outcome="Won",
                score="4-1",
                opponent=opp,
                tier_status="Povýšen na LT2",
                hrac=hrac,
                ign="mendu__",
                kit="MolePVP",
                bridge="LT2",
            ))
            return inter, rec_mock, grant_mock, commit_mock, grant_result

    @staticmethod
    def _record():
        return {
            "result": "created",
            "record": {
                "id": "x",
                "previousTier": "LT3",
                "newTier": "LT2",
                "bridgeTier": "LT2",
                "opponentId": "2",
                "opponentName": "souper",
                "date": "25.09.2026",
            },
        }

    def test_win_promotion_commits_exact_db_kwargs(self):
        inter, rec_mock, grant_mock, commit_mock, grant = self._call(
            self._record(),
            grant_result=TierRoleGrant(ok=True, verified=True, tier_role_id=202, note="ok"),
        )
        commit_mock.assert_awaited_once()
        kw = commit_mock.await_args.kwargs
        # bezprostřední spojení: celý grant jde do kanonické služby
        self.assertEqual(grant_mock.await_args.kwargs["tier_up"], "LT2")
        self.assertIs(kw["grant"], grant)
        self.assertEqual(kw["result_key"], "ht_fight:x")
        self.assertEqual(kw["kind"], "ht_fight")
        self.assertEqual(kw["discord_id"], 1)
        self.assertEqual(kw["ign"], "mendu__")
        self.assertEqual(kw["kit_key"], "MolePVP")
        self.assertEqual(kw["new_tier_code"], "LT2")
        # `discord_role_id` už neurčuje cog – vybírá ho služba z grantu
        self.assertNotIn("discord_role_id", kw)
        self.assertEqual(kw["previous_tier_code"], "LT3")
        self.assertEqual(kw["bridge_tier_code"], "LT2")
        self.assertEqual(kw["tier_status"], "Povýšen na LT2")
        self.assertEqual(kw["score"], "4-1")
        self.assertEqual(kw["outcome"], "Won")
        self.assertEqual(kw["opponent_id"], 2)
        self.assertEqual(kw["opponent_name"], "souper")
        self.assertEqual(kw["date"], "25.09.2026")
        self.assertEqual(kw["audit_actor_id"], inter.user.id)

    def test_grant_failed_is_handed_to_the_canonical_service(self):
        """Cog už NEMÁ vlastní gate – nepotvrzený grant se předá službě, která
        ho odmítne (viz test_grant_failed_never_writes_to_postgres v G0 testech)."""
        failed = TierRoleGrant(ok=False, note="nelze", tier_role_id=None)
        _, _, _, commit_mock, _ = self._call(self._record(), grant_result=failed)
        commit_mock.assert_awaited_once()
        self.assertIs(commit_mock.await_args.kwargs["grant"], failed)

    def test_legacy_empty_grant_is_handed_to_the_canonical_service(self):
        _, _, _, commit_mock, _ = self._call(self._record(), grant_result="")
        commit_mock.assert_awaited_once()
        self.assertEqual(commit_mock.await_args.kwargs["grant"], "")

    def test_unknown_bridge_code_still_commits_as_code(self):
        rec = self._record()
        rec["record"]["bridgeTier"] = "WR9"
        _, _, _, commit_mock, _ = self._call(
            rec,
            grant_result=TierRoleGrant(ok=True, verified=True, tier_role_id=202, note="ok"),
        )
        commit_mock.assert_awaited_once()
        self.assertEqual(commit_mock.await_args.kwargs["bridge_tier_code"], "WR9")

    def test_discord_grant_raises_hands_unconfirmed_grant_to_service(self):
        """Failure matrix 1/4: Discord nedostupný (grant RAISES) → žádný
        potvrzený grant, žádný dohad, hlasitá zpráva, oznámení pokračuje.
        Cog už gate nemá: předá službě `grant=None`, která to odmítne."""
        cm = self.cog_module
        with mock.patch.object(
            cm,
            "get_kits",
            new=mock.AsyncMock(return_value=["MolePVP"]),
        ), mock.patch.object(
            cm, "record_ht_fight", new=mock.AsyncMock(return_value=self._record())
        ), mock.patch.object(
            cm, "set_ht_fight_announcement", new=mock.AsyncMock(return_value={"result": "ok"})
        ), mock.patch.object(
            cm, "auto_grant_kit_role",
            new=mock.AsyncMock(
                side_effect=cm.discord.HTTPException(mock.MagicMock(), "discord down")
            ),
        ) as grant_mock, mock.patch.object(
            cm, "get_ticket", new=mock.AsyncMock(return_value=None)
        ), mock.patch(
            "db.services.commit_confirmed_promotion",
            new=mock.AsyncMock(return_value=SimpleNamespace(message="")),
        ) as commit_mock:
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=111))
            bot = mock.MagicMock()
            bot.get_channel.return_value = channel
            inter = mock.MagicMock()
            inter.response.defer = mock.AsyncMock()
            inter.followup.send = mock.AsyncMock()
            inter.user.name = "tester"
            inter.guild.get_role.return_value = mock.MagicMock(id=self.ROLE_ID)
            inter.channel_id = 9999

            cog = cm.TopResult(bot)
            hrac = mock.MagicMock(id=1, display_name="mendu__", name="mendu__")
            opp = mock.MagicMock(id=2, display_name="souper", name="souper")
            asyncio.run(cog.topresult.callback(
                cog,
                interaction=inter,
                fight_tier="HT3",
                outcome="Won",
                score="4-1",
                opponent=opp,
                tier_status="Povýšen na LT2",
                hrac=hrac,
                ign="mendu__",
                kit="MolePVP",
                bridge="LT2",
            ))
        grant_mock.assert_awaited_once()
        # žádný dohad: služba dostane grant=None (ne ok/verified za hráče)
        commit_mock.assert_awaited_once()
        self.assertIsNone(commit_mock.await_args.kwargs["grant"])
        channel.send.assert_not_awaited()
        reply = inter.followup.send.await_args.args[0]
        self.assertIn("nebylo potvrzeno", reply)
        self.assertIn("Tier roli se nepodařilo udělit", reply)

    def test_pg_unavailable_real_commit_surfaces_loud_message(self):
        """Failure matrix 2: PostgreSQL nedostupný PO úspěšném Discord grantu →
        skutečný commit_promotion_with_wedge vrátí hlasitou zprávu (bez wedge,
        Discord se NEvrací), reply ji obsahuje."""
        cm = self.cog_module
        with mock.patch.object(
            cm, "record_ht_fight", new=mock.AsyncMock(return_value=self._record())
        ), mock.patch.object(
            cm, "set_ht_fight_announcement", new=mock.AsyncMock(return_value={"result": "ok"})
        ), mock.patch.object(
            cm, "auto_grant_kit_role",
            new=mock.AsyncMock(
                return_value=TierRoleGrant(
                    ok=True, verified=True, tier_role_id=202, note="ok"
                )
            ),
        ) as grant_mock, mock.patch.object(
            cm, "get_ticket", new=mock.AsyncMock(return_value=None)
        ):
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=111))
            bot = mock.MagicMock()
            bot.db_session_factory = None
            bot.get_channel.return_value = channel
            inter = mock.MagicMock()
            inter.response.defer = mock.AsyncMock()
            inter.followup.send = mock.AsyncMock()
            inter.user.name = "tester"
            inter.guild.get_role.return_value = mock.MagicMock(id=self.ROLE_ID)
            inter.channel_id = 9999

            cog = cm.TopResult(bot)
            hrac = mock.MagicMock(id=1, display_name="mendu__", name="mendu__")
            opp = mock.MagicMock(id=2, display_name="souper", name="souper")
            asyncio.run(cog.topresult.callback(
                cog,
                interaction=inter,
                fight_tier="HT3",
                outcome="Won",
                score="4-1",
                opponent=opp,
                tier_status="Povýšen na LT2",
                hrac=hrac,
                ign="mendu__",
                kit="MolePVP",
                bridge="LT2",
            ))
        grant_mock.assert_awaited_once()
        reply = inter.followup.send.await_args.args[0]
        self.assertIn("PostgreSQL není nakonfigurováno", reply)
        self.assertIn("bez DB nelze ani outbox", reply)


class TopResultGuardTests(unittest.TestCase):
    """Self-result a neověřený soupeř se odmítají ještě před zápisem."""

    CHANNEL_ID = 5555
    ROLE_ID = 6666

    def setUp(self):
        self.cm = __import__("cogs.topresult", fromlist=["TopResult"])
        self._patches = [
            mock.patch.object(self.cm, "TOP_RESULT_CHANNEL_ID", self.CHANNEL_ID),
            mock.patch.object(self.cm, "TOP_RESULT_ROLE_ID", self.ROLE_ID),
            mock.patch.object(self.cm, "validate_topresult_config", return_value=(True, "")),
            mock.patch.object(self.cm, "is_registered_kit", return_value=True),
            mock.patch.object(self.cm, "get_kits", new=mock.AsyncMock(return_value=["MolePVP"])),
            mock.patch.object(self.cm, "get_ticket", new=mock.AsyncMock(return_value=None)),
        ]
        self.rec_mock = mock.AsyncMock()
        self._patches.append(mock.patch.object(self.cm, "record_ht_fight", new=self.rec_mock))
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def _call(self, *, author_id, player_id, opponent_id, opponent_is_tester, is_admin=False):
        cm = self.cm
        inter = mock.MagicMock()
        inter.user.id = author_id
        inter.response.defer = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        inter.channel_id = 9999
        inter.guild.get_role.return_value = mock.MagicMock(id=self.ROLE_ID)
        inter.guild.get_member.return_value = mock.MagicMock(id=opponent_id)
        bot = mock.MagicMock()
        bot.get_channel.return_value = mock.MagicMock()
        cog = cm.TopResult(bot)
        hrac = mock.MagicMock(id=player_id, display_name="p", name="p")
        opp = mock.MagicMock(id=opponent_id, display_name="o", name="o")
        tester_check = lambda m: m is not None and (  # noqa: E731
            m is inter.user or opponent_is_tester or m.id == author_id
        )
        with mock.patch.object(cm, "has_tester_role", side_effect=tester_check), \
             mock.patch.object(cm, "has_admin_role", return_value=is_admin):
            asyncio.run(cog.topresult.callback(
                cog, interaction=inter, fight_tier="HT3", outcome="Won",
                score="4-1", opponent=opp, tier_status="x", hrac=hrac,
                ign="mendu__", kit="MolePVP",
            ))
        return inter

    def test_tester_cannot_write_result_for_self(self):
        inter = self._call(author_id=1, player_id=1, opponent_id=2, opponent_is_tester=True)
        self.assertIn("sám sobě", inter.followup.send.await_args.args[0])
        self.rec_mock.assert_not_awaited()

    def test_opponent_must_be_a_tester(self):
        inter = self._call(author_id=5, player_id=1, opponent_id=2, opponent_is_tester=False)
        self.assertIn("Soupeř musí být tester", inter.followup.send.await_args.args[0])
        self.rec_mock.assert_not_awaited()

    def test_author_may_be_the_opponent(self):
        self.rec_mock.return_value = {"result": "invalid_tier", "message": "stop"}
        self._call(author_id=5, player_id=1, opponent_id=5, opponent_is_tester=False)
        self.rec_mock.assert_awaited_once()


class RetryViewDoubleClickTests(unittest.TestCase):
    def test_second_click_while_sending_is_ignored(self):
        cm = __import__("cogs.topresult", fromlist=["HTFightRetryView"])

        async def main():
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=1))
            view = cm.HTFightRetryView(
                result_id="x", result_channel=channel, content="c",
                allowed_mentions=None, session_factory=None,
            )
            view._sending = True
            inter = mock.MagicMock()
            inter.response.defer = mock.AsyncMock()
            with mock.patch.object(cm, "has_tester_role", return_value=True):
                await view.retry.callback(inter)
            channel.send.assert_not_awaited()
            inter.response.defer.assert_awaited_once()

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()

class NoJsonModeTests(unittest.TestCase):
    """services/topresult.py už NEMÁ JSON režim – vyžaduje PostgreSQL."""

    def test_record_ht_fight_requires_session_factory(self):
        async def main():
            with self.assertRaises(TypeError):
                await topresult.record_ht_fight(
                    player_id="1", player_name="mendu__", ign="mendu__",
                    evaluator_id="9", evaluator_name="tester", kit="MolePVP",
                    fight_tier="HT3", score="4-1", outcome="Won", opponent_id="2",
                    opponent_name="souper", tier_status="Povýšen na LT2",
                    now=NOW, date="23.09.2026",
                )
        asyncio.run(main())

    def test_readers_refuse_explicit_none(self):
        # Čtecí/oznamovací funkce s explicitním None odmítnou (RuntimeError),
        # nikdy nespadnou na ht_results.json.
        async def main():
            for coro in (
                topresult.set_ht_fight_announcement("2023", "sent", session_factory=None),
                topresult.get_ht_fight_result_for_ticket(2023, session_factory=None),
                topresult.get_ht_fight_results(session_factory=None),
            ):
                with self.assertRaises(RuntimeError, msg="PostgreSQL"):
                    await coro
        asyncio.run(main())

    def test_no_json_file_io_in_topresult(self):
        import inspect

        src = inspect.getsource(topresult)
        self.assertNotIn("load_data(", src)
        self.assertNotIn("save_data(", src)
        self.assertNotIn("if session_factory is not None:", src)
        self.assertNotIn("from services.store import transaction", src)



if __name__ == "__main__":
    unittest.main()

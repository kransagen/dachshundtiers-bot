"""F10 – odstranění TOCTOU v rozhodovací cestě HT3+ ticketu.

Co bylo špatně
--------------
``views.HT3Modal.on_submit`` si spočítal bránu (tier + eval) z
``players.json`` / ``evals.json`` ještě PŘED tím, než se čekalo na Discord
API (vyhledání člena, kategorie, ``create_text_channel``). Do
``create_ticket`` pak šly tyto hodnoty jen jako ``current_tier``/``eval_ok``
a funkce je měla vložit do ticketu naslepo. Mezi těmito dvěma kroky mohl
proběhnout ``/result`` (změna tieru) nebo ``/seteval`` + ``/uneval`` (změna
evalu) a ticket vznikl s neplatným oprávněním – a navíc s hodnotami, které
neodpovídají tomu, co je v ``players.json`` uložené teď.

Co F10 dělá
------------
``create_ticket`` si uvnitř své transakce znovu přečte ``players.json`` a
``evals.json`` (pod týmiž zámky, pod kterými pak zapisuje) a porovná to s
volajícím. Při shodě uloží autoritativní hodnoty; při rozdílu vrátí
``{"result": "tier_changed"}`` a nic nezaloží.

Co testy ZÁMĚRNĘ netvrdí
-------------------------
Že by se „vracelo zpět" – hráč prostě zopakuje pokus a brána se přepočítá
z čerstvých dat. Vznik sice zanechá krátké okno, kdy byl vytvořen kanál, který
se hned smaže, ale žádný ticket s neplatným oprávněním v DB nezůstane.
"""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

import discord

import storage
import utils
import views
from services import tickets
from tests import json_backend_only

NOW = 1_700_000_000_000
COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000

OWNER_ID = "1"
IGN = "AliceMC"
KIT = "AnchorPvP"


def _set_inputs(modal, ign: str, tier: str) -> None:
    modal.ign_input._value = ign
    modal.tier_input._value = tier


def _interaction(guild=None):
    inter = mock.MagicMock()
    inter.guild = guild or mock.MagicMock()
    inter.user.id = 111
    inter.user.name = "alice"
    inter.user.display_name = "alice"
    inter.response.defer = mock.AsyncMock()
    inter.response.send_message = mock.AsyncMock()
    inter.followup.send = mock.AsyncMock()
    return inter


@json_backend_only("brána se revaliduje nad players.json/evals.json v tempdiru")
class GateFixture(unittest.TestCase):
    """Hráč s tierem LT3 a evalem → brána propouští (a data jdou do tempdir)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        for name, empty in (
            (tickets.HT_TICKETS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
        ):
            storage.save_data(name, empty)
        self.give_player(tier="LT3", eval_ok=True)

    # --- stavové manipulace (simulují /result, /seteval, /uneval) -----------
    def give_player(self, *, tier="LT3", eval_ok=True, username=IGN, owner=OWNER_ID):
        players = storage.load_data(tickets.PLAYERS_FILE, []) or []
        players = [p for p in players if str(p.get("discordId") or "") != owner]
        if tier is not None:
            players.append(
                {"username": username, "discordId": owner, "modes": {KIT: tier}}
            )
        storage.save_data(tickets.PLAYERS_FILE, players)

        evals = storage.load_data(utils.EVALS_FILE, {}) or {}
        bucket = evals.get(KIT.lower(), {})
        if eval_ok:
            bucket[IGN.lower()] = NOW
        else:
            bucket.pop(IGN.lower(), None)
        if bucket:
            evals[KIT.lower()] = bucket
        else:
            evals.pop(KIT.lower(), None)
        storage.save_data(utils.EVALS_FILE, evals)

    def drop_player(self):
        players = storage.load_data(tickets.PLAYERS_FILE, []) or []
        players = [p for p in players if str(p.get("discordId") or "") != OWNER_ID]
        storage.save_data(tickets.PLAYERS_FILE, players)

    # --- helpery -----------------------------------------------------------
    def authoritative(self):
        players = storage.load_data(tickets.PLAYERS_FILE, []) or []
        evals = storage.load_data(utils.EVALS_FILE, {}) or {}
        return (
            tickets.player_tier_from(players, IGN, KIT, OWNER_ID),
            utils.eval_in(evals, IGN, KIT),
        )

    def create(self, channel_id=1001, *, current_tier="LT3", eval_ok=True, **kw):
        """Volání create_ticket se snapshotem brány, jak ho dělá views."""
        params = {
            "channel_id": channel_id,
            "owner_id": OWNER_ID,
            "owner_name": "alice",
            "ign": IGN,
            "kit": KIT,
            "target_tier": "HT3",
            "current_tier": current_tier,
            "eval_ok": eval_ok,
            "category_id": 555,
            "now": NOW,
        }
        params.update(kw)
        return tickets.create_ticket(**params)

    def stored(self):
        return storage.load_data(tickets.HT_TICKETS_FILE, {})


# ---------------------------------------------------------------------------
# 1) Stav se změní v okně mezi výpočtem brány a zápisem → zamítnuto
# ---------------------------------------------------------------------------
class TierChangedDuringWindowTests(GateFixture):
    def test_tier_dropped_during_window_is_rejected(self):
        async def main():
            # snapshot brány: LT3 + eval (takhle to spočítá views)
            snapshot_tier, snapshot_eval = self.authoritative()
            self.assertEqual((snapshot_tier, snapshot_eval), ("LT3", True))

            # ... mezitím proběhne /result a hráč spadne na LT5
            self.give_player(tier="LT5", eval_ok=True)

            result = await self.create(current_tier=snapshot_tier, eval_ok=snapshot_eval)
            self.assertEqual(result["result"], "tier_changed")
            self.assertEqual(result["current_tier"], "LT5")
            self.assertEqual(result["eval_ok"], True)
            self.assertEqual(self.stored(), {}, "ticket se nesmí uložit")

        asyncio.run(main())

    def test_eval_revoked_during_window_is_rejected(self):
        async def main():
            snapshot_tier, snapshot_eval = self.authoritative()
            # ... mezitím projde /uneval
            self.give_player(tier="LT3", eval_ok=False)

            result = await self.create(current_tier=snapshot_tier, eval_ok=snapshot_eval)
            self.assertEqual(result["result"], "tier_changed")
            self.assertEqual(result["current_tier"], "LT3")
            self.assertEqual(result["eval_ok"], False)
            self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def test_eval_revoked_and_tier_kept_is_rejected(self):
        async def main():
            self.give_player(tier="LT3", eval_ok=False)
            result = await self.create(current_tier="LT3", eval_ok=True)
            self.assertEqual(result["result"], "tier_changed")
            self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def test_player_record_removed_during_window_is_rejected(self):
        """Hr��č zmizel z players.json → nesmí vzniknout ticket „bez tieru"."""

        async def main():
            self.drop_player()
            result = await self.create(current_tier="LT3", eval_ok=True)
            self.assertEqual(result["result"], "tier_changed")
            self.assertIsNone(result["current_tier"])
            self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def test_tier_promotion_during_window_is_also_rejected(self):
        """Konzervativní pravidlo: zamítneme JAKOUKOLI změnu, ne jen pokles.

        Povýšení by hráči nepřekáželo, ale snapshot se stejně stal neplatným
        a jeho kontrola limitu ``target_tier`` už nemusí sedět. Lepší je
        odmítnout a nechat hráče pokus zopakovat.
        """

        async def main():
            self.give_player(tier="HT2", eval_ok=True)
            result = await self.create(current_tier="LT3", eval_ok=True)
            self.assertEqual(result["result"], "tier_changed")
            self.assertEqual(result["current_tier"], "HT2")
            self.assertEqual(self.stored(), {})

        asyncio.run(main())


# ---------------------------------------------------------------------------
# 2) Beze změny se chová dřív jako dřív
# ---------------------------------------------------------------------------
class UnchangedStateTests(GateFixture):
    def test_unchanged_valid_state_creates_ticket(self):
        async def main():
            result = await self.create()
            self.assertEqual(result["result"], "created")
            self.assertIn("1001", self.stored())
            self.assertEqual(self.stored()["1001"]["currentTier"], "LT3")
            self.assertEqual(self.stored()["1001"]["eval"], True)

        asyncio.run(main())

    def test_rewriting_the_same_tier_does_not_reject(self):
        """Nepovede se zamítnout kvůli /result, který zapsal stejný tier.

        Porovnává se hodnota, ne identita zápisu – běžné přepisování dat
        nesmí hráči překážet v otevření ticketu.
        """

        async def main():
            self.give_player(tier="LT3", eval_ok=True)  # /result se stejným výsledkem
            result = await self.create()
            self.assertEqual(result["result"], "created")

        asyncio.run(main())

    def test_ticket_never_disagrees_with_authoritative_state(self):
        """Invariant: uložený ticket nikdy nesouhlasí s players.json/evals.json.

        Otestováno přes všechny varianty TOCTOU – v každé musí buď vzniknout
        ticket shodný s autoritativním stavem, nebo žádný.
        """

        async def main():
            scenarios = [
                ("demote", lambda: self.give_player(tier="LT5")),
                ("promote", lambda: self.give_player(tier="HT2")),
                ("uneval", lambda: self.give_player(eval_ok=False)),
                ("drop", self.drop_player),
                ("drop+demote", lambda: (self.drop_player(), self.give_player(tier="LT5"))),
            ]
            for i, (_name, mutate) in enumerate(scenarios):
                with self.subTest(scenario=_name):
                    mutate()
                    before = self.authoritative()
                    cid = 2000 + i
                    result = await self.create(
                        channel_id=cid, current_tier="LT3", eval_ok=True
                    )
                    if result["result"] == "created":
                        ticket = self.stored()[str(cid)]
                        self.assertEqual(
                            (ticket["currentTier"], ticket["eval"]), before
                        )
                    else:
                        self.assertEqual(result["result"], "tier_changed")
                        self.assertNotIn(str(cid), self.stored())

        asyncio.run(main())


# ---------------------------------------------------------------------------
# 3) Chyba čtení = žádný zápis (strict, ne „nemá tier")
# ---------------------------------------------------------------------------
class GateReadFailureTests(GateFixture):
    def _path(self, name):
        return os.path.join(self._tmp, name)

    def test_corrupt_players_json_blocks_ticket(self):
        async def main():
            with open(self._path(tickets.PLAYERS_FILE), "w", encoding="utf-8") as fh:
                fh.write("{ rozbitina")
            with self.assertRaises(storage.DataCorruptionError):
                await self.create()
            self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def test_corrupt_evals_json_blocks_ticket(self):
        async def main():
            with open(self._path(utils.EVALS_FILE), "w", encoding="utf-8") as fh:
                fh.write("{ rozbitina")
            with self.assertRaises(storage.DataCorruptionError):
                await self.create()
            self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def _path(self, name):
        import os

        return os.path.join(self._tmp, name)


# ---------------------------------------------------------------------------
# 4) Duplicate / cooldown mají přednost a chovají se dál stejně
# ---------------------------------------------------------------------------
class DuplicateAndCooldownPrecedenceTests(GateFixture):
    def test_duplicate_wins_over_changed_gate(self):
        async def main():
            await self.create(channel_id=1001)
            # ... brána se mezitím změní; existující ticket má hlášku přednost
            self.give_player(tier="LT5")
            result = await self.create(channel_id=1002, cooldown_ms=0)
            self.assertEqual(result["result"], "duplicate")
            self.assertEqual(result["ticket"]["id"], "1001")
            self.assertNotIn("1002", self.stored())

        asyncio.run(main())

    def test_cooldown_wins_over_changed_gate(self):
        async def main():
            storage.save_data(
                tickets.HT3_COOLDOWNS_FILE, {OWNER_ID: {KIT: NOW + COOLDOWN_MS}}
            )
            self.give_player(tier="LT5")
            result = await self.create(cooldown_ms=COOLDOWN_MS)
            self.assertEqual(result["result"], "cooldown")
            self.assertEqual(result["remaining_ms"], COOLDOWN_MS)
            self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def test_duplicate_still_blocked_with_valid_gate(self):
        async def main():
            await self.create(channel_id=1001)
            result = await self.create(channel_id=1002)
            self.assertEqual(result["result"], "duplicate")
            self.assertEqual(len(self.stored()), 1)

        asyncio.run(main())

    def test_cooldown_still_blocked_with_valid_gate(self):
        async def main():
            storage.save_data(
                tickets.HT3_COOLDOWNS_FILE, {OWNER_ID: {KIT: NOW + COOLDOWN_MS}}
            )
            result = await self.create(cooldown_ms=COOLDOWN_MS, now=NOW + 5)
            self.assertEqual(result["result"], "cooldown")
            self.assertEqual(result["remaining_ms"], COOLDOWN_MS - 5)
            self.assertEqual(self.stored(), {})

        asyncio.run(main())


# ---------------------------------------------------------------------------
# 5) Skutečná souběžnost + uvolnění zámku po zamítnutí
# ---------------------------------------------------------------------------
class ConcurrencyTests(GateFixture):
    def test_concurrent_change_never_stores_a_stale_gate(self):
        """Souběžné /result a create_ticket: buď vznikne shodný ticket, nebo žádný.

        Pořadí je nedeterministické a to je v pořádku – běží-li create první,
        ticket je v okamžiku svého zápisu legitimní. Podstatné je, že se
        NIKDY neuloží ticket, který by s aktuálním stavem nesouhlasil.
        """

        async def change_tier():
            await asyncio.sleep(0)
            self.give_player(tier="LT5")

        async def main():
            before = self.authoritative()
            result, _ = await asyncio.gather(self.create(), change_tier())
            self.assertIn(result["result"], {"created", "tier_changed"})
            if result["result"] == "created":
                self.assertEqual(self.stored()["1001"]["currentTier"], "LT3")
                self.assertEqual(self.stored()["1001"]["eval"], before[1])
            else:
                self.assertEqual(self.stored(), {})

        asyncio.run(main())

    def test_rejection_releases_locks_for_next_attempt(self):
        """Po zamítnutí musí být store použitelný – žádný mrtvý zámek."""

        async def main():
            self.give_player(tier="LT5")
            first = await self.create(channel_id=1001)
            self.assertEqual(first["result"], "tier_changed")
            # hráč se opraví a zkusí to znovu
            self.give_player(tier="LT3", eval_ok=True)
            second = await self.create(channel_id=1002)
            self.assertEqual(second["result"], "created")
            self.assertIn("1002", self.stored())

        asyncio.run(main())


# ---------------------------------------------------------------------------
# 6) View vrstva: žádný sirotek kanál ani duch v DB
# ---------------------------------------------------------------------------
class ModalOrphanTests(GateFixture):
    def setUp(self):
        super().setUp()
        for target, new in (
            ("get_ht3_ticket_category", mock.Mock(return_value=777)),
            ("_apply_ticket_overwrites", mock.Mock(return_value={})),
        ):
            patcher = mock.patch.object(views, target, new)
            patcher.start()
            self.addCleanup(patcher.stop)
        # find_player_tier / has_eval / find_open_ticket / create_ticket jsou
        # VĚDOMĚ skutečné – testuje se celá cesta včetně revalidace.

    def _guild(self, channel, on_create=None):
        """Guild, který při vytvoření kanálu volá ``on_create``.

        ``on_create`` je háček na „okno mezi bránou a zápisem“: AsyncMock
        použije návratovou hodnotu side_effectu, takže musí vrátit kanál.
        """
        if on_create is not None:
            hook = on_create

            def _side_effect(**kwargs):
                hook(**kwargs)
                return channel

            create = mock.AsyncMock(side_effect=_side_effect)
        else:
            create = mock.AsyncMock(return_value=channel)
        guild = mock.MagicMock()
        guild.get_channel.side_effect = (
            lambda cid: mock.MagicMock() if cid == 777 else None
        )
        guild.create_text_channel = create
        return guild

    def test_tier_change_during_discord_await_deletes_channel(self):
        async def main():
            channel = mock.MagicMock()
            channel.id = 123
            channel.delete = mock.AsyncMock()
            channel.send = mock.AsyncMock()

            def on_create(**kwargs):
                # přesně to se děje MEZI výpočtem brány a zápisem:
                # create_text_channel je await, po jeho návratu se píše do DB
                self.give_player(tier="LT5")

            guild = self._guild(channel, on_create=on_create)
            modal = views.HT3Modal(KIT)
            _set_inputs(modal, IGN, "HT3")
            inter = _interaction(guild)

            await modal.on_submit(inter)

            channel.delete.assert_awaited_once()
            self.assertEqual(self.stored(), {}, "v DB nesmí zůstat duch")
            msg = inter.followup.send.await_args.args[0]
            self.assertIn("změnil tvůj tier nebo eval", msg)
            self.assertIn("LT5", msg)

        asyncio.run(main())

    def test_eval_revoke_during_discord_await_deletes_channel(self):
        async def main():
            channel = mock.MagicMock()
            channel.id = 123
            channel.delete = mock.AsyncMock()
            channel.send = mock.AsyncMock()

            def on_create(**kwargs):
                self.give_player(eval_ok=False)

            guild = self._guild(channel, on_create=on_create)
            modal = views.HT3Modal(KIT)
            _set_inputs(modal, IGN, "HT3")
            inter = _interaction(guild)

            await modal.on_submit(inter)

            channel.delete.assert_awaited_once()
            self.assertEqual(self.stored(), {})
            msg = inter.followup.send.await_args.args[0]
            self.assertIn("změnil tvůj tier nebo eval", msg)
            # hráč si LT3 ponechal, ztratil jen eval – hláška to vypíše
            self.assertIn("LT3", msg)

        asyncio.run(main())

    def test_unchanged_state_still_creates_channel_and_ticket(self):
        async def main():
            channel = mock.MagicMock()
            channel.id = 123
            channel.delete = mock.AsyncMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=999))
            guild = self._guild(channel)
            patcher = mock.patch.object(
                views, "ticket_embed", mock.Mock(return_value=mock.MagicMock())
            )
            patcher.start()
            self.addCleanup(patcher.stop)
            # view.add_view je reálný a v testu nemá klienta
            inter = _interaction(guild)
            inter.client = mock.MagicMock()

            modal = views.HT3Modal(KIT)
            _set_inputs(modal, IGN, "HT3")
            await modal.on_submit(inter)

            channel.delete.assert_not_awaited()
            self.assertIn("123", self.stored())
            self.assertEqual(self.stored()["123"]["currentTier"], "LT3")
            self.assertEqual(self.stored()["123"]["eval"], True)
            msg = inter.followup.send.await_args.args[0]
            self.assertIn("Ticket byl vytvořen", msg)

        asyncio.run(main())

    def test_channel_deleted_even_when_delete_itself_fails(self):
        """Odmítnutí nesmí spadnout kvůli chybě mazání – jinak by kanál zůstal."""

        async def main():
            channel = mock.MagicMock()
            channel.id = 123
            channel.delete = mock.AsyncMock(
                side_effect=discord.Forbidden(
                    mock.MagicMock(status=403, reason="no"), "no"
                )
            )

            guild = self._guild(
                channel, on_create=lambda **kw: self.give_player(tier="LT5")
            )
            modal = views.HT3Modal(KIT)
            _set_inputs(modal, IGN, "HT3")
            inter = _interaction(guild)

            await modal.on_submit(inter)

            channel.delete.assert_awaited_once()
            self.assertEqual(self.stored(), {})
            inter.followup.send.assert_awaited()

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()

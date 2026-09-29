"""Atomicita fronty, evalu a /skip (F8, F7, F6).

- F8 – záznam vytaženého hráče se nesmí ztratit: /add v legacy tester roomce
  zapisuje přes transakci, takže souběžný pull (i chybný default místo
  poškozeného souboru) jeho záznam nepřepíše.
- F7 – „LT3 + eval" patří do stejné transakce jako tier a historie; selhání
  uvnitř transakce nesmí nechat částečný zápis.
- F6 – /skip nesmí držet zámky store/DB přes Discord awaity a před finálním
  zápisem musí znovu ověřit, že záznam o vytažení mezitím nezmizel.

Bez reálného Discordu: interaction/guild/kanál jsou MagicMock/AsyncMock,
storage.DATA_DIR ukazuje na tempdir.
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import discord

import cogs.ht3 as ht3
import cogs.queues as queues
import storage
import utils
from services import queue_service, results, store
from services import tickets
from tests import json_backend_only

NOW = 1_700_000_000_000
QUEUE_COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000
PLAYER_ID = "1"
IGN = "AliceMC"
KIT = "AnchorPvP"


def _patch(tc: unittest.TestCase, module, name, new) -> None:
    patcher = mock.patch.object(module, name, new)
    patcher.start()
    tc.addCleanup(patcher.stop)


def _member(uid: int = 1, name: str = "AliceMC"):
    m = mock.MagicMock()
    m.id = uid
    m.display_name = name
    m.voice = None
    return m


def _pulled_entry(channel="777", kit=KIT, ign=IGN, uid=PLAYER_ID):
    return {
        "channel": str(channel),
        "player": {
            "id": str(uid),
            "username": ign,
            "ign": ign,
            "kit": kit,
            "joinedAt": NOW,
        },
    }


def _queue_entry(uid="2", kit=KIT, ign="BobMC"):
    return {"id": str(uid), "username": ign, "ign": ign, "kit": kit, "joinedAt": NOW}


def _text_channel(channel_id=777):
    return mock.MagicMock(spec=discord.TextChannel, id=channel_id)


class TempDataDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)


# ---------------------------------------------------------------- F8
@json_backend_only("fixture pulled_players.json v tempdiru přes DATA_DIR")
class AddPulledPlayerAtomicityTests(TempDataDirMixin):
    """F8 – /add v legacy tester roomce nesmí přepsat cizí záznam."""

    def setUp(self):
        super().setUp()
        storage.save_data("pulled_players.json", {})
        _patch(self, ht3, "has_tester_role", mock.Mock(return_value=True))
        _patch(self, ht3, "get_ticket", mock.AsyncMock(return_value=None))

    def _interaction(self, channel, guild):
        inter = mock.MagicMock()
        inter.guild = guild
        inter.channel = channel
        inter.user.id = 111
        inter.user.display_name = "tester"
        inter.response.send_message = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        return inter

    def test_concurrent_pull_record_survives_add(self):
        """Souběžný pull během /add nesmí být přepsán."""

        async def main():
            # pull, který se "dokončí" právě když /add čeká na Discordu
            other = _queue_entry(uid="2", ign="BobMC")
            storage.save_data("queue.json", [other])

            async def concurrent_pull(*_args, **_kwargs):
                await queue_service.pop_for_kit_with_pulled(KIT, 777)

            channel = _text_channel(777)
            channel.set_permissions = mock.AsyncMock(side_effect=concurrent_pull)
            inter = self._interaction(channel, mock.MagicMock())

            cog = ht3.HT3(mock.MagicMock())
            await cog.add.callback(cog, interaction=inter, hrac=_member())

            pulled = storage.load_data("pulled_players.json", {})
            self.assertIn(str(_member().id), pulled)
            self.assertIn("2", pulled)
            self.assertEqual(pulled["2"]["channel"], "777")
            self.assertEqual(storage.load_data("queue.json"), [])

        asyncio.run(main())

    def test_corrupt_pulled_file_is_not_overwritten_with_default(self):
        """Poškozený pulled_players.json se nesmí přepsat defaultem {}."""

        async def main():
            broken = "{ tohle neni json"
            with open(
                f"{self._tmp}/pulled_players.json", "w", encoding="utf-8"
            ) as fh:
                fh.write(broken)

            channel = _text_channel(777)
            channel.set_permissions = mock.AsyncMock()
            inter = self._interaction(channel, mock.MagicMock())

            cog = ht3.HT3(mock.MagicMock())
            with self.assertRaises(storage.DataCorruptionError):
                await cog.add.callback(cog, interaction=inter, hrac=_member())

            with open(
                f"{self._tmp}/pulled_players.json", encoding="utf-8"
            ) as fh:
                self.assertEqual(fh.read(), broken)

        asyncio.run(main())


# ---------------------------------------------------------------- F7
class EvalResultAtomicityTests(TempDataDirMixin):
    """F7 – eval + tier + historie musí být jedna transakce."""

    def setUp(self):
        super().setUp()
        for name, default in (
            ("players.json", []),
            ("cooldowns.json", {}),
            (utils.EVALS_FILE, {}),
            (results.HT_RESULTS_FILE, {}),
            (tickets.HT_TICKETS_FILE, {}),
            (tickets.HT3_COOLDOWNS_FILE, {}),
            (tickets.HT_TICKET_LOGS_FILE, {}),
        ):
            storage.save_data(name, default)
        self.now = NOW

    async def _record(self, **overrides):
        kwargs = dict(
            player_id=PLAYER_ID,
            player_name="alice",
            ign=IGN,
            evaluator_id="9",
            evaluator_name="bob",
            kit=KIT,
            new_tier="LT3",
            display_tier="LT3",
            score="5-2",
            outcome="Won",
            notes="",
            now=self.now,
            date="23.09.2026",
            queue_cooldown_ms=QUEUE_COOLDOWN_MS,
        )
        kwargs.update(overrides)
        return await results.record_result(**kwargs)

    def test_eval_written_together_with_tier_and_history(self):
        async def main():
            rec = await self._record(eval_flag=True)

            self.assertEqual(rec["result"], "created")
            self.assertTrue(rec["eval_applied"])
            self.assertEqual(
                storage.load_data(utils.EVALS_FILE),
                {KIT.lower(): {IGN.lower(): NOW}},
            )

            players = storage.load_data("players.json")
            self.assertEqual(players[0]["modes"], {KIT: "LT3"})
            self.assertEqual(len(storage.load_data(results.HT_RESULTS_FILE)), 1)

        asyncio.run(main())

    def test_eval_failure_rolls_back_whole_result(self):
        """Selhání evalu uvnitř transakce = žádný částečný zápis."""

        async def main():
            def boom(*_args, **_kwargs):
                raise RuntimeError("eval write failed")

            _patch(self, results, "apply_eval_status", boom)

            with self.assertRaises(RuntimeError):
                await self._record(eval_flag=True)

            self.assertEqual(storage.load_data("players.json"), [])
            self.assertEqual(storage.load_data(results.HT_RESULTS_FILE), {})
            self.assertEqual(storage.load_data(utils.EVALS_FILE), {})
            self.assertEqual(storage.load_data("cooldowns.json"), {})

        asyncio.run(main())

    def test_eval_flag_false_does_not_touch_evals(self):
        """/topresult (eval_flag=False) nesmí sahat na evals.json."""

        async def main():
            storage.save_data(utils.EVALS_FILE, {KIT: {"Somebody": NOW}})
            rec = await self._record(eval_flag=False)

            self.assertEqual(rec["result"], "created")
            self.assertFalse(rec["eval_applied"])
            self.assertEqual(
                storage.load_data(utils.EVALS_FILE), {KIT: {"Somebody": NOW}}
            )
            self.assertEqual(storage.load_data("players.json")[0]["modes"], {KIT: "LT3"})

        asyncio.run(main())

    def test_set_eval_helper_shares_normalization(self):
        """set_eval a transakční cesta používají stejnou normalizaci."""

        storage.save_data(utils.EVALS_FILE, {})
        self.assertTrue(utils.set_eval("  alicemc ", "AnchorPvP"))
        self.assertIn(IGN.lower(), storage.load_data(utils.EVALS_FILE)[KIT.lower()])

        evals = {}
        self.assertTrue(utils.apply_eval_status(evals, "ALICEMC", KIT, NOW))
        self.assertEqual(evals, {KIT.lower(): {IGN.lower(): NOW}})

        untouched = {KIT.lower(): {IGN.lower(): NOW}}
        self.assertFalse(utils.apply_eval_status(untouched, "", KIT, NOW))
        self.assertEqual(untouched, {KIT.lower(): {IGN.lower(): NOW}})


# ---------------------------------------------------------------- F6
class SkipLockScopeTests(TempDataDirMixin):
    """F6 – /skip nesmí držet zámky přes Discord awaity."""

    def setUp(self):
        super().setUp()
        storage.save_data("pulled_players.json", {PLAYER_ID: _pulled_entry()})
        storage.save_data("queue.json", [_queue_entry(uid="2", ign="BobMC")])
        _patch(self, queues, "has_tester_role", mock.Mock(return_value=True))
        self.panel = mock.AsyncMock()
        _patch(self, queues, "update_panel", self.panel)

    def _channel(self, side_effect=None):
        channel = mock.MagicMock()
        channel.id = 777
        channel.set_permissions = mock.AsyncMock(side_effect=side_effect)
        return channel

    async def _skip(self, channel):
        inter = mock.MagicMock()
        inter.guild = mock.MagicMock()
        inter.guild.get_channel.return_value = channel
        inter.guild.fetch_channel = mock.AsyncMock(return_value=channel)
        inter.user.id = 111
        inter.user.display_name = "tester"
        inter.response.send_message = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        cog = queues.Queues(mock.MagicMock())
        await cog.skip.callback(cog, interaction=inter, hrac=_member())
        return inter

    def test_no_store_lock_held_during_discord_await(self):
        async def main():
            seen = {}

            async def inspect(*_args, **_kwargs):
                seen["pulled"] = store._file_lock("pulled_players.json").locked()
                seen["queue"] = store._file_lock("queue.json").locked()

            await self._skip(self._channel(inspect))

            self.assertEqual(seen, {"pulled": False, "queue": False})
            self.assertEqual(storage.load_data("pulled_players.json", {}), {})
            self.assertEqual(
                [p["id"] for p in storage.load_data("queue.json")], ["2", "1"]
            )

        asyncio.run(main())

    def test_state_change_during_discord_is_revalidated(self):
        """Mezitím zpracovaný hráč se nesmí vrátit do fronty."""

        async def main():
            async def consumed_by_result(*_args, **_kwargs):
                await queue_service.remove_pulled_player(PLAYER_ID)

            inter = await self._skip(self._channel(consumed_by_result))

            self.assertIn("mezitím změnil", inter.response.send_message.await_args.args[0])
            self.assertEqual(
                storage.load_data("queue.json"), [_queue_entry(uid="2", ign="BobMC")]
            )
            self.assertEqual(storage.load_data("pulled_players.json", {}), {})

        asyncio.run(main())



if __name__ == "__main__":
    unittest.main()

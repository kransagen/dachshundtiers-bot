"""Regresní testy F5b – strict čtení v zbývajících produkčních writerech.

Problém
-------
Writer typu „přečti, uprav, ulož" (``read-modify-write``) s ne-strict
``load_data()`` je destruktivní: při selhání čtení dostane default, provede
úpravu nad defaultem a ULOŽÍ ho. Výsledkem je zápis odvozený z nečitelného
stavu, který přepíše platná data. Těžší je to u JSON backendu, kde čtení
opravdu může selhat (poškozený soubor); u PostgreSQL to selhání čtení
simuluje výjimka z :func:`storage.postgres_load`.

Co testy dokazují
------------------
Pro každý opravený writer: při selhání čtení se NEZAPÍŠE NIC a chyba
propaguje (nejde o „chyt chybu a ulož default"). Platná data zůstávají
dokonale zachovaná – porovnává se i surový obsah souboru, ne jen parsovaný
výsledek.

Co testy ZÁMĚRNĚ netvrdí
------------------------
F5b NEMÁNI čtenářům. ``get_kits()`` a ``get_evals()`` bez ``strict`` dál
vrací default – čtení si s fallbackem legitimně smí (autocomplete, panely).
To je ověřeno samostatně, aby se „oprava“ nezvrátila v přehnanou striktnost.
"""

import asyncio
import os
import tempfile
import unittest
import uuid
from unittest import mock

import storage
import utils
from services import store
from tests import json_backend_only

_RUN_ID = uuid.uuid4().hex[:8]

BROKEN_JSON = "{ tohle neni json"


@json_backend_only("corrupt JSON soubor v tempdiru; s PostgreSQL by šlo o jinou věc")
class JsonCorruptMixin:
    """Připraví poškozený soubor a po testu ověří, že se nezměnil."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _path(self, name: str) -> str:
        return os.path.join(self._tmp, name)

    def _write_broken(self, name: str) -> None:
        with open(self._path(name), "w", encoding="utf-8") as fh:
            fh.write(BROKEN_JSON)

    def _write_valid(self, name: str, data) -> None:
        storage.save_data(name, data)

    def _raw(self, name: str) -> str:
        with open(self._path(name), encoding="utf-8") as fh:
            return fh.read()

    def _assert_untouched(self, name: str, msg: str = "") -> None:
        self.assertEqual(
            self._raw(name), BROKEN_JSON, f"{name} byl přepsán. {msg}"
        )


# --------------------------------------------------------------- utils: kity
class KitsWriterStrictReadTests(JsonCorruptMixin, unittest.TestCase):
    """utils.add_kit / remove_kit nesmí zapsat výchozí seznam nad poškozenými daty."""

    def test_add_kit_aborts_on_corrupt_kits_json(self):
        self._write_broken("kits.json")
        with self.assertRaises(storage.DataCorruptionError):
            utils.add_kit("NovýKit")
        self._assert_untouched("kits.json", "add_kit nesmí zapsat DEFAULT_KITS.")

    def test_remove_kit_aborts_on_corrupt_kits_json(self):
        self._write_broken("kits.json")
        with self.assertRaises(storage.DataCorruptionError):
            utils.remove_kit("AnchorPvP")
        self._assert_untouched("kits.json", "remove_kit nesmí zapsat prázdný seznam.")

    def test_valid_state_is_not_overwritten_with_default(self):
        """Kontrola, že nepřepisujeme defaultem: cizí kity zůstanou."""
        payload = ["AnchorPvP", "MolePVP"]
        self._write_valid("kits.json", payload)
        self.assertTrue(utils.add_kit("UHCMace"))
        self.assertEqual(
            storage.load_data("kits.json"), ["AnchorPvP", "MolePVP", "UHCMace"]
        )

    def test_duplicate_kit_still_reports_false(self):
        self._write_valid("kits.json", ["AnchorPvP"])
        self.assertFalse(utils.add_kit("anchorpvp"), "duplikát musí vrátit False")
        self.assertEqual(storage.load_data("kits.json"), ["AnchorPvP"])

    def test_remove_missing_kit_still_reports_false(self):
        self._write_valid("kits.json", ["AnchorPvP"])
        self.assertFalse(utils.remove_kit("Neexistujici"))
        self.assertEqual(storage.load_data("kits.json"), ["AnchorPvP"])

    def test_remove_existing_kit_still_works(self):
        self._write_valid("kits.json", ["AnchorPvP", "MolePVP"])
        self.assertTrue(utils.remove_kit("molepvp"), "case-insensitive remove")
        self.assertEqual(storage.load_data("kits.json"), ["AnchorPvP"])

    def test_reader_stays_lenient_by_default(self):
        """Čtenář (autocomplete/panely) si smí default ponechat.

        Pozor na přesný tvar fallbacku: ``DEFAULT_KITS`` se používá jen když
        soubor NEEXISTUJE. Nečitelný soubor vrací default předaný v kódu,
        tedy prázdný seznam – to je dřívější chování a F5b ho nechává být.
        Právě proto je destructive add_kit: `[] + [nový kit]` by přepsalo
        všechny ostatní kity.
        """
        self._write_broken("kits.json")
        with self.assertLogs("dachshundtiers", level="ERROR"):
            self.assertEqual(utils.get_kits(), [])

    def test_missing_file_still_falls_back_to_default_kits(self):
        """Chybějící soubor není korupce – tady je fallback na DEFAULT_KITS správný."""
        self.assertEqual(utils.get_kits(), list(utils.DEFAULT_KITS))

    def test_reader_is_strict_on_request(self):
        self._write_broken("kits.json")
        with self.assertRaises(storage.DataCorruptionError):
            utils.get_kits(strict=True)


# -------------------------------------------------------------- utils: evaly
class EvalsWriterStrictReadTests(JsonCorruptMixin, unittest.TestCase):
    """utils.set_eval / unset_eval nesmí zapsat {} nad poškozenými daty.

    Tady je destruktivita nejostřejší: prázdný dict by smazal VŠECHNY evaly.
    """

    def test_set_eval_aborts_on_corrupt_evals_json(self):
        self._write_broken(utils.EVALS_FILE)
        with self.assertRaises(storage.DataCorruptionError):
            utils.set_eval("AliceMC", "AnchorPvP")
        self._assert_untouched(utils.EVALS_FILE, "set_eval nesmí zapsat {}.")

    def test_unset_eval_aborts_on_corrupt_evals_json(self):
        self._write_broken(utils.EVALS_FILE)
        with self.assertRaises(storage.DataCorruptionError):
            utils.unset_eval("AliceMC", "AnchorPvP")
        self._assert_untouched(utils.EVALS_FILE, "unset_eval nesmí zapsat {}.")

    def test_set_eval_keeps_other_kits_on_success(self):
        now = utils.now_ms()
        self._write_valid(
            utils.EVALS_FILE, {"molepvp": {"bobmc": now}, "uhcmace": {"carolmc": now}}
        )
        self.assertTrue(utils.set_eval(" AliceMC ", "AnchorPvP"))
        stored = storage.load_data(utils.EVALS_FILE)
        self.assertEqual(set(stored), {"molepvp", "uhcmace", "anchorpvp"})
        self.assertIn("bobmc", stored["molepvp"])
        self.assertIn("carolmc", stored["uhcmace"])
        self.assertIn("alicemc", stored["anchorpvp"])

    def test_unset_eval_keeps_other_entries_on_success(self):
        now = utils.now_ms()
        self._write_valid(
            utils.EVALS_FILE, {"anchorpvp": {"alicemc": now, "bobmc": now}}
        )
        self.assertTrue(utils.unset_eval("alicemc", "anchorpvp"))
        self.assertEqual(storage.load_data(utils.EVALS_FILE), {"anchorpvp": {"bobmc": now}})

    def test_unset_missing_eval_still_reports_false(self):
        self._write_valid(utils.EVALS_FILE, {"anchorpvp": {"bobmc": 1}})
        self.assertFalse(utils.unset_eval("neexistuje", "anchorpvp"))

    def test_has_eval_reader_stays_lenient(self):
        """has_eval je čtenář – poškozený soubor znamená „nemá eval"."""
        self._write_broken(utils.EVALS_FILE)
        with self.assertLogs("dachshundtiers", level="ERROR"):
            self.assertFalse(utils.has_eval("AliceMC", "AnchorPvP"))


# ------------------------------------------------------------- config: kanály
class QueueChannelWriterStrictReadTests(JsonCorruptMixin, unittest.TestCase):
    """config.set_queue_channel_id nesmí zapsat jen jeden kit nad {}."""

    def test_set_queue_channel_aborts_on_corrupt_file(self):
        import config

        self._write_broken(config.QUEUE_CHANNELS_FILE)
        with self.assertRaises(storage.DataCorruptionError):
            config.set_queue_channel_id("anchorpvp", 123)
        self._assert_untouched(
            config.QUEUE_CHANNELS_FILE,
            "set_queue_channel_id nesmí zapsat jen nový kit.",
        )

    def test_set_queue_channel_keeps_other_kits_on_success(self):
        import config

        self._write_valid(config.QUEUE_CHANNELS_FILE, {"molepvp": 111, "uhcmace": 222})
        config.set_queue_channel_id("AnchorPvP", 333)
        self.assertEqual(
            storage.load_data(config.QUEUE_CHANNELS_FILE),
            {"molepvp": 111, "uhcmace": 222, "anchorpvp": 333},
        )

    def test_get_queue_channel_id_reader_stays_lenient(self):
        """Čtenář smí spadnout na env/default – jen nepotřebuje přesnou hodnotu."""
        import config

        self._write_broken(config.QUEUE_CHANNELS_FILE)
        with self.assertLogs("dachshundtiers", level="ERROR"):
            fallback = config.get_queue_channel_id("anchorpvp")
        self.assertEqual(fallback, config.QUEUE_CHANNELS.get("anchorpvp"))


# ------------------------------------------- cogs/queues: /openq, joinastester
class QueueCogWriterStrictReadTests(JsonCorruptMixin, unittest.TestCase):
    """Transakční writery v cogs/queues.py čtou strict přes tx.get()."""

    def _joinastester_tx(self, user_id: str):
        async def _join(tx):
            testers = tx.get("testers.json")
            if user_id not in testers:
                testers.append(user_id)
            tx.set("testers.json", testers)
            return None

        return store.transaction(("testers.json",), _join)

    def test_joinastester_aborts_on_corrupt_testers_json(self):
        self._write_broken("testers.json")
        with self.assertRaises(storage.DataCorruptionError):
            asyncio.run(self._joinastester_tx("42"))
        self._assert_untouched(
            "testers.json", "zápis by přepsal všechny aktivní testery."
        )

    def test_joinastester_keeps_existing_testers_on_success(self):
        self._write_valid("testers.json", ["1", "2"])
        asyncio.run(self._joinastester_tx("3"))
        self.assertEqual(storage.load_data("testers.json"), ["1", "2", "3"])

    def test_joinastester_is_idempotent(self):
        self._write_valid("testers.json", ["1"])
        asyncio.run(self._joinastester_tx("1"))
        self.assertEqual(storage.load_data("testers.json"), ["1"])

    def test_openq_panel_write_aborts_on_corrupt_json(self):
        self._write_broken("queue_messages.json")

        async def _remember_panel(tx):
            panels = tx.get("queue_messages.json", {})
            panels["anchorpvp"] = {"message_id": "1", "kit": "AnchorPvP"}
            tx.set("queue_messages.json", panels)
            return None

        with self.assertRaises(storage.DataCorruptionError):
            asyncio.run(store.transaction(("queue_messages.json",), _remember_panel))
        self._assert_untouched(
            "queue_messages.json", "zápis by ztratil panely ostatních kitů."
        )

    def test_openq_panel_write_keeps_other_panels_on_success(self):
        self._write_valid(
            "queue_messages.json", {"molepvp": {"message_id": "9", "kit": "MolePVP"}}
        )

        async def _remember_panel(tx):
            panels = tx.get("queue_messages.json", {})
            panels["anchorpvp"] = {"message_id": "1", "kit": "AnchorPvP"}
            tx.set("queue_messages.json", panels)
            return None

        asyncio.run(store.transaction(("queue_messages.json",), _remember_panel))
        stored = storage.load_data("queue_messages.json")
        self.assertEqual(set(stored), {"molepvp", "anchorpvp"})
        self.assertEqual(stored["molepvp"]["message_id"], "9")


# ------------------------------------------- cogs/tournaments + views signup
class TournamentWriterStrictReadTests(JsonCorruptMixin, unittest.TestCase):
    """Writery turnajů nesmějí zapsat default {} nad poškozenými daty."""

    def test_mark_ended_aborts_on_corrupt_file(self):
        self._write_broken("tournaments.json")

        async def _mark_ended(tx):
            tournaments = tx.get("tournaments.json", {})
            tdata = tournaments.get("anchorpvp")
            if not tdata or tdata.get("ended"):
                return None
            tdata["ended"] = True
            tx.set("tournaments.json", tournaments)
            return tournaments

        with self.assertRaises(storage.DataCorruptionError):
            asyncio.run(store.transaction(("tournaments.json",), _mark_ended))
        self._assert_untouched("tournaments.json")

    def test_create_aborts_on_corrupt_file(self):
        self._write_broken("tournaments.json")

        async def _create(tx):
            current = tx.get("tournaments.json", {})
            current["anchorpvp"] = {"kit": "AnchorPvP", "ended": False}
            tx.set("tournaments.json", current)
            return None

        with self.assertRaises(storage.DataCorruptionError):
            asyncio.run(store.transaction(("tournaments.json",), _create))
        self._assert_untouched("tournaments.json")

    def test_delete_aborts_on_corrupt_file(self):
        self._write_broken("tournaments.json")

        async def _delete(tx):
            current = tx.get("tournaments.json", {})
            current.pop("anchorpvp", None)
            tx.set("tournaments.json", current)
            return None

        with self.assertRaises(storage.DataCorruptionError):
            asyncio.run(store.transaction(("tournaments.json",), _delete))
        self._assert_untouched("tournaments.json")

    def test_signup_aborts_on_corrupt_file(self):
        """Přihlášení tlačítkem: chyba čtení musí spadnout PŘED zápisem."""
        self._write_broken("tournaments.json")
        outcome_holder = {}

        async def _signup(tx):
            tournaments = tx.get("tournaments.json", {})
            tdata = tournaments.get("anchorpvp")
            if not tdata or tdata.get("ended"):
                return "ended"
            tdata.setdefault("participants", []).append("42")
            tx.set("tournaments.json", tournaments)
            return "joined"

        async def run():
            outcome_holder["value"] = await store.transaction(
                ("tournaments.json",), _signup
            )

        with self.assertRaises(storage.DataCorruptionError):
            asyncio.run(run())
        self.assertNotIn("value", outcome_holder)
        self._assert_untouched("tournaments.json")

    def test_signup_keeps_other_tournaments_on_success(self):
        self._write_valid(
            "tournaments.json",
            {
                "molepvp": {"participants": ["1"], "ended": False},
                "anchorpvp": {"participants": [], "ended": False},
            },
        )

        async def _signup(tx):
            tournaments = tx.get("tournaments.json", {})
            tdata = tournaments.get("anchorpvp")
            if not tdata or tdata.get("ended"):
                return "ended"
            tdata.setdefault("participants", []).append("42")
            tx.set("tournaments.json", tournaments)
            return "joined"

        self.assertEqual(
            asyncio.run(store.transaction(("tournaments.json",), _signup)), "joined"
        )
        stored = storage.load_data("tournaments.json")
        self.assertEqual(set(stored), {"molepvp", "anchorpvp"})
        self.assertEqual(stored["anchorpvp"]["participants"], ["42"])
        self.assertEqual(stored["molepvp"]["participants"], ["1"])


# --------------------------------------------------- simulovaná chyba čtení
class ReadFailureNotSwallowedTests(JsonCorruptMixin, unittest.TestCase):
    """Writer nesmí výjimku čtení spolknout a uložit default.

    Tady chybu neprodukuje poškozený soubor, ale výjimka z backendu – stejná
    situace jako nedostupná tabulka v PostgreSQL. Chytit by ji nebylo.
    """

    def test_save_data_never_called_after_read_failure(self):
        """Writer nesmí výjimku čtení spolknout a zapsat default.

        Patchuje se ``utils.load_data``/``utils.save_data``, ne ``storage.*`` –
        moduly importují funkce přes ``from storage import ...``, takže
        náhrada v ``storage`` by je vůbec nezasáhla.
        """
        for call, name in (
            (lambda: utils.add_kit("NovýKit"), "kits.json"),
            (lambda: utils.set_eval("AliceMC", "AnchorPvP"), utils.EVALS_FILE),
            (lambda: utils.unset_eval("AliceMC", "AnchorPvP"), utils.EVALS_FILE),
        ):
            with self.subTest(file=name):
                self._write_valid(name, {"sentinel": True})
                before = self._raw(name)
                with mock.patch.object(
                    utils, "load_data", side_effect=OSError("read failed")
                ):
                    with mock.patch.object(utils, "save_data") as save:
                        with self.assertRaises(OSError):
                            call()
                save.assert_not_called()
                self.assertEqual(self._raw(name), before, "soubor byl přepsán")

    def test_corrupt_file_via_unicode_error(self):
        """Nejen JSONDecodeError, ale i nečitelná data (např. špatné kódování)."""
        path = self._path("kits.json")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe\x00 invalid utf-8 \x80\x81")
        with self.assertRaises(storage.DataCorruptionError):
            utils.add_kit("NovýKit")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"\xff\xfe\x00 invalid utf-8 \x80\x81")


if __name__ == "__main__":
    unittest.main()

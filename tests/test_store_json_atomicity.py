"""Regresní testy F11 – dvoufázové ukládání v JSON backendu.

Problém, který F11 řeší
------------------------
``store.transaction`` dřív volal ``save_data`` soubor po souboru. Když selhal
zápis třetího souboru, první dva už ležely na disku – backend pak měl jinou
sémantiku než PostgreSQL, i když vypadal stejně. Testy níže ověřují, že po
selhání *přípravy* dat zůstane každý cílový soubor nedotčený.

Co testy ZÁMĚRNĚ netvrdí
-----------------------
Plná vícesouborová atomicita na POSIX není dosažitelná a F11 ji netvrdí.
``os.replace`` je atomický jednotlivě, ale N přesunů za sebou ne. Selhání až
v samotné ``os.replace`` fázi tedy zanechá část souborů nových – to je
dokumentované omezení v ``services.store._commit_json`` a
``test_replacement_failure_is_reported_as_non_atomic`` ho zachycuje místo
tichého předstírání, že je vše v pořádku.
"""

import asyncio
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import storage
from services import store
from tests import json_backend_only


@json_backend_only("F11 řeší dvoufázové ukládání JSON souborů (temp files, práva, formát)")
class TempDataDirMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _path(self, name: str) -> str:
        return os.path.join(self._tmp, name)

    def _read(self, name: str) -> str:
        """Surowý obsah souboru – chceme vidět i formátování, ne jen data."""
        with open(self._path(name), encoding="utf-8") as fh:
            return fh.read()

    def _seed(self, name: str, data) -> None:
        storage.save_data(name, data)

    def _tmp_leftovers(self) -> list:
        return sorted(
            f for f in os.listdir(self._tmp) if f.endswith(".tmp")
        )

    def _dir_listing(self) -> list:
        return sorted(os.listdir(self._tmp))


# ---------------------------------------------------------------- 1. commit
class JsonTransactionCommitTests(TempDataDirMixin):
    """Úspěšná transakce stále uloží všechny změněné soubory."""

    def test_successful_multi_file_transaction_commits_every_file(self):
        names = ("a.json", "b.json", "c.json")
        for name in names:
            self._seed(name, {"old": True})
        self._seed("untouched.json", {"keep": 1})

        async def main():
            async def fn(tx):
                for i, name in enumerate(names):
                    tx.set(name, {"n": i, "payload": [1, 2, 3]})
                return "ok"

            return await store.transaction(names, fn)

        self.assertEqual(asyncio.run(main()), "ok")
        for i, name in enumerate(names):
            self.assertEqual(
                storage.load_data(name), {"n": i, "payload": [1, 2, 3]}
            )
        self.assertEqual(self._tmp_leftovers(), [])

    def test_commit_of_brand_new_files_creates_them(self):
        names = ("fresh1.json", "fresh2.json")

        async def main():
            async def fn(tx):
                tx.set(names[0], [1])
                tx.set(names[1], {"x": "y"})

            return await store.transaction(names, fn)

        asyncio.run(main())
        self.assertEqual(self._dir_listing(), ["fresh1.json", "fresh2.json"])
        self.assertEqual(storage.load_data(names[0]), [1])
        self.assertEqual(storage.load_data(names[1]), {"x": "y"})

    def test_formatting_and_unicode_are_unchanged(self):
        """Staging nesmí změnit kódování ani formát oproti save_data."""
        payload = {"jméno": "český hráč 🐕", "nested": {"a": [1, None, True]}}
        self._seed("f.json", payload)

        async def main():
            async def fn(tx):
                tx.set("f.json", payload)

            return await store.transaction(("f.json",), fn)

        asyncio.run(main())

        expected = json.dumps(payload, ensure_ascii=False, indent=2)
        self.assertEqual(self._read("f.json"), expected)
        # ensure_ascii=False: znaky musí zůstat čitelné, ne jako \uXXXX
        self.assertIn("český hráč 🐕", self._read("f.json"))
        self.assertNotIn("\\u", self._read("f.json"))

    def test_file_mode_behaviour_is_unchanged_by_f11(self):
        """Pozor na zaměněný požadavek: F11 práva NEmění oproti minulosti.

        Dřív i teď vzniká dočasný soubor s právy podle umask a ``os.replace``
        je přenese na cíl, takže už existující ``0o600`` zůstane ``0o644``.
        Tady se to záměrně píní jako „nezměněné", aby někdo nepovažoval
        zachování práv za součást F11 – není a nikdy nebylo. Cílem je, aby to
        nezůstalo tiché překvapení, kdyby se to někdy začalo měnit.
        """
        # Očekávané práva závisí na umask, ne pevně na 0o644.
        probe = os.path.join(self._tmp, ".probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
        expected_mode = stat.S_IMODE(os.stat(probe).st_mode)
        os.remove(probe)
        path = self._path("perm.json")
        self._seed("perm.json", {"v": 1})
        os.chmod(path, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

        async def main():
            async def fn(tx):
                tx.set("perm.json", {"v": 2})

            return await store.transaction(("perm.json",), fn)

        asyncio.run(main())

        # Stejné jako před F11: práva se přenesou z dočasného souboru.
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), expected_mode)
        self.assertEqual(storage.load_data("perm.json"), {"v": 2})


# ------------------------------------------------- 2. selhání při přípravě
class JsonStagingFailureTests(TempDataDirMixin):
    """Selhání při přípravě pozdějšího souboru nesmí změnit žádný cíl."""

    def test_failure_staging_later_file_leaves_all_targets_unchanged(self):
        names = ("a.json", "b.json", "c.json")
        before = {name: {"v": f"old-{name}"} for name in names}
        for name, value in before.items():
            self._seed(name, value)
        self._seed("untouched.json", {"keep": 1})
        snapshots = {name: self._read(name) for name in before}

        async def main():
            async def fn(tx):
                for name in names:
                    tx.set(name, {"v": f"new-{name}"})
                return "neprobe"

            return await store.transaction(names, fn)

        # Fáze 1 připravuje soubory v seřazeném pořadí, takže names[2] je
        # poslední připravovaný soubor – selhání u něj musí zrušit i ty před ním.
        real_stage = store.stage_data
        seen: list = []

        def flaky(name, data):
            if name == names[2]:
                raise OSError("disk full")
            seen.append(name)
            return real_stage(name, data)

        with mock.patch.object(store, "stage_data", flaky):
            with self.assertRaises(OSError):
                asyncio.run(main())

        # Klíčové tvrzení: ani jeden cílový soubor se nesměl změnit.
        for name in names:
            self.assertEqual(
                self._read(name),
                snapshots[name],
                f"{name} byl změněn i po selhání přípravy jiného souboru",
            )
            self.assertEqual(storage.load_data(name), before[name])
        self.assertEqual(storage.load_data("untouched.json"), {"keep": 1})
        self.assertNotIn(names[2], seen)
        # Selhání nastalo až u POSLEDNÍHO souboru, ne na prvním – jinak by
        # test neprokázal, že je chráněn i stav po částečné přípravě.
        self.assertEqual(sorted(seen), sorted(names[:2]))

    def test_unserializable_data_aborts_whole_transaction(self):
        """Typická příprava-dat chyba: nejson-serializovatelná hodnota."""
        names = ("x.json", "y.json")
        for name in names:
            self._seed(name, {"v": 0})

        async def main():
            async def fn(tx):
                tx.set(names[0], {"v": 1})
                tx.set(names[1], {"bad": {1, 2, 3}})  # set není JSON
                return "neprobe"

            return await store.transaction(names, fn)

        with self.assertRaises(TypeError):
            asyncio.run(main())

        self.assertEqual(storage.load_data(names[0]), {"v": 0})
        self.assertEqual(storage.load_data(names[1]), {"v": 0})
        # json.dump píše postupně, takže napůl zapsaný dočasný soubor musí
        # zmizet taky – jinak by v data/ zůstala směs.
        self.assertEqual(self._tmp_leftovers(), [])

    def test_callback_exception_never_stages_anything(self):
        """Chyba v fn nastane před stagin[gem – cíle se nedotknou vůbec."""
        names = ("p.json", "q.json")
        for name in names:
            self._seed(name, {"v": 0})
        stage = mock.Mock(side_effect=AssertionError("nesmí se volat"))
        boom = RuntimeError("selhání testerovy operace")

        async def main():
            async def fn(tx):
                tx.set(names[0], {"v": 1})
                raise boom

            return await store.transaction(names, fn)

        with mock.patch.object(store, "stage_data", stage):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(main())

        self.assertIs(ctx.exception, boom)
        self.assertEqual(stage.call_count, 0)
        for name in names:
            self.assertEqual(storage.load_data(name), {"v": 0})

    def test_replacement_failure_is_reported_as_non_atomic(self):
        """Selhání až v ``os.replace`` je NEAKOMBINATICKÉ a musí být vidět.

        Tady je záměrně očekáván dílčí zápis: první ``os.replace`` uspěje,
        druhý selže. Test existuje, aby se toto omezení nezapalo jako „fix" a
        aby chování bylo zdokumentované, ne překvapivé.
        """
        names = ("m1.json", "m2.json")
        for name in names:
            self._seed(name, {"v": "old"})

        real_replace = os.replace
        calls: list = []

        def flaky_replace(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("replace failed")
            return real_replace(src, dst)

        async def main():
            async def fn(tx):
                for name in names:
                    tx.set(name, {"v": "new"})
                return "neprobe"

            return await store.transaction(names, fn)

        with mock.patch.object(os, "replace", flaky_replace):
            with self.assertRaises(OSError):
                asyncio.run(main())

        self.assertEqual(len(calls), 2, "os.replace má být voláno pro oba soubory")
        self.assertEqual(self._tmp_leftovers(), [], "dočasné soubory musí zmizet")


# ------------------------------------------------------------ 3. cleanup
class JsonStagingCleanupTests(TempDataDirMixin):
    """Dočasné soubory nesmějí zůstat v data/ – ani po úspěchu, ani po chybě."""

    def test_no_temp_files_after_successful_transaction(self):
        names = ("s1.json", "s2.json")

        async def main():
            async def fn(tx):
                tx.set(names[0], {"a": 1})
                tx.set(names[1], {"b": 2})
                return "ok"

            return await store.transaction(names, fn)

        asyncio.run(main())
        self.assertEqual(self._tmp_leftovers(), [])
        self.assertEqual(self._dir_listing(), ["s1.json", "s2.json"])

    def test_no_temp_files_after_staging_failure(self):
        names = ("f1.json", "f2.json", "f3.json")
        for name in names:
            self._seed(name, {"v": 0})
        real_stage = store.stage_data

        def flaky(name, data):
            if name == names[2]:
                raise OSError("disk full")
            return real_stage(name, data)

        async def main():
            async def fn(tx):
                for name in names:
                    tx.set(name, {"v": 1})
                return "neprobe"

            return await store.transaction(names, fn)

        with mock.patch.object(store, "stage_data", flaky):
            with self.assertRaises(OSError):
                asyncio.run(main())

        self.assertEqual(self._tmp_leftovers(), [])
        self.assertEqual(self._dir_listing(), sorted(names))

    def test_no_temp_files_after_single_file_save_failure(self):
        """save_data (jednosouborová cesta) si směs taky nechá."""
        self._seed("one.json", {"v": 1})
        with mock.patch.object(storage.os, "replace", side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                storage.save_data("one.json", {"v": 2})
        self.assertEqual(self._tmp_leftovers(), [])
        self.assertEqual(storage.load_data("one.json"), {"v": 1})

    def test_no_temp_files_after_single_file_serialization_failure(self):
        """save_data neuchová napůl zapsaný dočasný soubor."""
        self._seed("two.json", {"v": 1})
        with self.assertRaises(TypeError):
            storage.save_data("two.json", {"bad": {1, 2, 3}})
        self.assertEqual(self._tmp_leftovers(), [])
        self.assertEqual(storage.load_data("two.json"), {"v": 1})

    def test_read_only_file_in_directory_ignores_other_files(self):
        """Kontrola je specifická pro .tmp – cizí soubory v data/ nechá být."""
        with open(self._path("keepme.txt"), "w", encoding="utf-8") as fh:
            fh.write("ne临时 soubor")

        async def main():
            async def fn(tx):
                tx.set("only.json", {"v": 1})
                return "ok"

            return await store.transaction(("only.json",), fn)

        asyncio.run(main())
        self.assertEqual(self._tmp_leftovers(), [])
        self.assertIn("keepme.txt", self._dir_listing())


# ------------------------------------------- 4. zachování existujícího chování
class JsonTransactionBehaviorTests(TempDataDirMixin):
    """Regresní pojistka: F11 nesmí rozbít nic, co fungovalo dřív."""

    def test_return_value_and_error_propagation_intact(self):
        async def main():
            def fn(tx):
                tx.set("r.json", {"hit": True})
                return 7

            result = await store.transaction(("r.json",), fn)
            self.assertEqual(result, 7)
            self.assertEqual(await store.read("r.json", {}), {"hit": True})

        asyncio.run(main())

    def test_error_inside_callback_still_propagates_unchanged(self):
        self._seed("e.json", {"v": 1})
        boom = ValueError("konkrétní chyba")

        async def main():
            async def fn(tx):
                tx.set("e.json", {"v": 2})
                raise boom

            return await store.transaction(("e.json",), fn)

        with self.assertRaises(ValueError) as ctx:
            asyncio.run(main())
        self.assertIs(ctx.exception, boom)
        self.assertEqual(storage.load_data("e.json"), {"v": 1})

    def test_transaction_merges_with_existing_content(self):
        self._seed("m.json", {"keep": True})

        async def main():
            async def fn(tx):
                data = tx.get("m.json", {})
                data["added"] = 1
                tx.set("m.json", data)
                return "merged"

            return await store.transaction(("m.json",), fn)

        self.assertEqual(asyncio.run(main()), "merged")
        self.assertEqual(storage.load_data("m.json"), {"keep": True, "added": 1})

    def test_locks_released_and_usable_after_failure(self):
        """Po chybě musí jít nad stejnými soubory znovu – jinak by to viselo."""
        names = ("lock.json",)

        async def failing():
            async def fn(tx):
                tx.set(names[0], {"v": "bad", "s": {1, 2}})
                return "neprobe"

            return await store.transaction(names, fn)

        with self.assertRaises(TypeError):
            asyncio.run(failing())

        async def retry():
            async def fn(tx):
                tx.set(names[0], {"v": "ok"})
                return "ok"

            return await asyncio.wait_for(store.transaction(names, fn), timeout=5)

        self.assertEqual(asyncio.run(retry()), "ok")
        self.assertEqual(storage.load_data("lock.json"), {"v": "ok"})

    def test_untouched_file_is_not_created(self):
        read_only = "ro.json"
        written = "wr.json"

        async def main():
            async def fn(tx):
                self.assertEqual(tx.get(read_only, {"default": True}), {"default": True})
                tx.set(written, {"n": 1})
                return "ok"

            return await store.transaction((read_only, written), fn)

        self.assertEqual(asyncio.run(main()), "ok")
        self.assertFalse(os.path.exists(self._path(read_only)))
        self.assertEqual(storage.load_data(written), {"n": 1})

    def test_get_outside_declared_files_raises_value_error(self):
        covered = "in.json"
        outside = "out.json"

        async def main():
            async def fn(tx):
                with self.assertRaises(ValueError):
                    tx.get(outside, {})
                with self.assertRaises(ValueError):
                    tx.set(outside, {})
                return "checked"

            return await store.transaction((covered,), fn)

        self.assertEqual(asyncio.run(main()), "checked")
        self.assertFalse(os.path.exists(self._path(outside)))


if __name__ == "__main__":
    unittest.main()

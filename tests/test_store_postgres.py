"""Testy transakčního úložiště services/store.py nad REÁLNÝM PostgreSQL backendem.

Proč tento soubor existuje
--------------------------
Všechny ostatní testy běží nad JSON backendem a přepisují si
``storage.DATA_DIR`` na temp adresář. PostgreSQL cestu (``postgres_load``,
``postgres_save``, ``postgres_lock_keys`` a ``store.transaction`` nad DB) tedy
nikdo nikdy netestoval – a to je právě backend, který README doporučuje pro
produkci a ve kterém je vícesouborová transakce skutečně atomická (viz F11).

Přeskočení vs. selhání
-----------------------
* ``DATABASE_URL`` **není** nastavená → třída se přeskočí výslovně, jednou
  zřetelnou zprávou. To je běžný lokální běh bez DB.
* ``DATABASE_URL`` **je** nastavená, ale databáze neodpovídá → test **SELHÁ**,
  ne přeskočí. V CI to znamená rozbité PostgreSQL, ne „nemáme DB“.

Izolace: každý test pracuje s vlastními klíči ``pgt_<run>_<test>_<n>``, které
``tearDown`` smaže, takže se testy nepletou s sebou ani s reálnými daty ve
stejné databázi. Schéma se nemění – používá tabulku, kterou založí
``storage._ensure_postgres_schema()``."""

import asyncio
import threading
import unittest
import uuid
from unittest import mock

import storage
from services import store

_RUN_ID = uuid.uuid4().hex[:8]
_KEY_PREFIX = f"pgt_{_RUN_ID}_"

_TIMEOUT = 20.0
# Pojistka proti zavěšení CI, kdyby se zámek neočistil: test pak selhá, ne visí.
_MUST_STILL_BLOCK = 0.5

_PG_CONFIGURED = bool(storage.DATABASE_URL.strip())


@unittest.skipUnless(
    _PG_CONFIGURED,
    "DATABASE_URL není nastavená → PostgreSQL testy přeskočeny (lokální běh bez DB). "
    "V CI se spouštějí povinně: viz job `postgres` v .github/workflows/ci.yml.",
)
class PostgresStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Nakonfigurovaná, ale nedostupná databáze je CHYBA, ne důvod ke skipu.
        # Výjimku necháme propadnout, aby ve výstupu byla skutečná příčina.
        with storage.postgres_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    def setUp(self):
        self._seq = 0
        self.addCleanup(self._purge)

    def _key(self, suffix: str) -> str:
        self._seq += 1
        return f"{_KEY_PREFIX}{self._testMethodName}_{self._seq}_{suffix}"

    def _purge(self) -> None:
        with storage.postgres_connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {storage._POSTGRES_TABLE} WHERE key LIKE %s",
                (f"{_KEY_PREFIX}%",),
            )

    def _raw(self, key: str):
        """Čte přímo SQL z jiného spojení, takže vidí výhradně COMMITOVANÝ stav."""
        with storage.postgres_connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT value FROM {storage._POSTGRES_TABLE} WHERE key = %s", (key,)
            )
            row = cur.fetchone()
        return None if row is None else row[0]

    def _exists(self, key: str) -> bool:
        return self._raw(key) is not None

    def _seed(self, key: str, value) -> None:
        with storage.postgres_connection() as conn:
            storage.postgres_save(conn, key, value)

    def test_two_file_transaction_commits_both_changes(self):
        k1 = self._key("a.json")
        k2 = self._key("b.json")

        async def main():
            async def fn(tx):
                self.assertEqual(tx.get(k1, {}), {})
                self.assertEqual(tx.get(k2, []), [])
                data = tx.get(k1, {})
                data["tier"] = "LT3"
                tx.set(k1, data)
                tx.set(k2, ["a", "b"])
                return "committed"

            return await store.transaction((k1, k2), fn)

        self.assertEqual(asyncio.run(main()), "committed")
        self.assertEqual(self._raw(k1), {"tier": "LT3"})
        self.assertEqual(self._raw(k2), ["a", "b"])

    def test_two_file_transaction_merges_with_existing_records(self):
        """Dotčený soubor se čte, ne přepisuje defaultem – cizí záznamy zůstanou."""
        k1 = self._key("a.json")
        k2 = self._key("b.json")
        self._seed(k1, {"keep": True})
        self._seed(k2, ["keep"])

        async def main():
            async def fn(tx):
                first = tx.get(k1, {})
                first["n"] = 1
                tx.set(k1, first)
                second = tx.get(k2, [])
                second.append("new")
                tx.set(k2, second)
                return "merged"

            return await store.transaction((k1, k2), fn)

        self.assertEqual(asyncio.run(main()), "merged")
        self.assertEqual(self._raw(k1), {"keep": True, "n": 1})
        self.assertEqual(self._raw(k2), ["keep", "new"])

    def test_failure_during_write_rolls_back_whole_transaction(self):
        k1 = self._key("a.json")
        k2 = self._key("b.json")
        # Výchozí stav, aby šlo poznat, že změna k1 se opravdu ZRUŠILA,
        # a ne jen „nikdy nenastala“.
        self._seed(k1, {"n": 0})

        async def main():
            async def fn(tx):
                tx.set(k1, {"n": 1})
                # `set` není JSON-serializovatelný, takže postgres_save vyhodí
                # TypeError uprostřed commitu. Reálná chyba backendu, žádný mock.
                tx.set(k2, {1, 2, 3})
                return "sem se nesmí dostat"

            return await store.transaction((k1, k2), fn)

        psycopg = storage._psycopg()
        with self.assertRaises((TypeError, psycopg.Error)):
            asyncio.run(main())

        # Transaction._dirty je `set`, takže pořadí zápisů není garantované.
        # Tvrzení je proto nezávislé na pořadí: selhání jednoho zápisu ruší OBA.
        self.assertFalse(self._exists(k2), "neúspěšný záznam nesmí vzniknout")
        self.assertEqual(
            self._raw(k1), {"n": 0}, "změna prvního souboru musí být zrušena"
        )

    def test_failure_during_write_releases_advisory_lock(self):
        k1 = self._key("a.json")
        k2 = self._key("b.json")

        async def failing():
            async def fn(tx):
                tx.set(k1, {"n": 1})
                tx.set(k2, {1, 2, 3})
                return "neprobe"

            return await store.transaction((k1, k2), fn)

        with self.assertRaises((TypeError, storage._psycopg().Error)):
            asyncio.run(failing())

        acquired: list = []

        def waiter():
            with storage.postgres_connection(autocommit=False) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET lock_timeout = '10s'")
                storage.postgres_lock_keys(conn, [k1, k2])
                acquired.append(True)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(timeout=_TIMEOUT)
        self.assertFalse(thread.is_alive(), "zámek zůstal zavěšený po rollbacku")
        self.assertEqual(acquired, [True], "zámek se po rollbacku neuvolnil")

    def test_postgres_lock_keys_serializes_concurrent_connections(self):
        key = self._key("lock.json")
        holder_locked = threading.Event()
        waiter_trying = threading.Event()
        waiter_acquired = threading.Event()
        may_release = threading.Event()
        errors: list = []

        def holder():
            try:
                with storage.postgres_connection(autocommit=False) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET lock_timeout = '30s'")
                    storage.postgres_lock_keys(conn, [key])
                    holder_locked.set()
                    may_release.wait(timeout=_TIMEOUT)
                    conn.rollback()
            except Exception as err:  # noqa: BLE001 – chybu chceme vidět v assertu
                errors.append(("holder", err))
                holder_locked.set()

        def waiter():
            try:
                holder_locked.wait(timeout=_TIMEOUT)
                with storage.postgres_connection(autocommit=False) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET lock_timeout = '30s'")
                    waiter_trying.set()
                    storage.postgres_lock_keys(conn, [key])
                    waiter_acquired.set()
            except Exception as err:  # noqa: BLE001
                errors.append(("waiter", err))
                waiter_acquired.set()

        threads = [threading.Thread(target=fn, daemon=True) for fn in (holder, waiter)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(holder_locked.wait(timeout=_TIMEOUT))
            self.assertTrue(waiter_trying.wait(timeout=_TIMEOUT))
            # Držitel stále drží zámek → druhé spojení MUSÍ být zablokované.
            self.assertFalse(
                waiter_acquired.wait(timeout=_MUST_STILL_BLOCK),
                "druhé spojení nebylo serializováno – získalo zámek pod drženým",
            )
            self.assertEqual(errors, [], "chyba při čekání na zámek")
        finally:
            may_release.set()
            for thread in threads:
                thread.join(timeout=_TIMEOUT)

        self.assertTrue(
            waiter_acquired.wait(timeout=_TIMEOUT),
            "čekatel nezískal zámek ani po uvolnění držitele",
        )
        for thread in threads:
            self.assertFalse(thread.is_alive(), "vlákno zůstalo viset")

    def test_advisory_locks_are_per_key(self):
        k1 = self._key("one.json")
        k2 = self._key("two.json")
        first_locked = threading.Event()
        may_release = threading.Event()
        second_acquired = threading.Event()
        errors: list = []

        def holder():
            try:
                with storage.postgres_connection(autocommit=False) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET lock_timeout = '30s'")
                    storage.postgres_lock_keys(conn, [k1])
                    first_locked.set()
                    may_release.wait(timeout=_TIMEOUT)
                    conn.rollback()
            except Exception as err:  # noqa: BLE001
                errors.append(("holder", err))
                first_locked.set()

        def other():
            try:
                first_locked.wait(timeout=_TIMEOUT)
                with storage.postgres_connection(autocommit=False) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET lock_timeout = '10s'")
                    storage.postgres_lock_keys(conn, [k2])
                    second_acquired.set()
            except Exception as err:  # noqa: BLE001
                errors.append(("other", err))
                second_acquired.set()

        threads = [threading.Thread(target=fn, daemon=True) for fn in (holder, other)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(first_locked.wait(timeout=_TIMEOUT))
            self.assertTrue(
                second_acquired.wait(timeout=_MUST_STILL_BLOCK * 4),
                "zámek jiného klíče byl zabytečně zablokován",
            )
            self.assertEqual(errors, [], "chyba při získávání nezávislého zámku")
        finally:
            may_release.set()
            for thread in threads:
                thread.join(timeout=_TIMEOUT)

    def test_transaction_takes_advisory_locks_for_all_declared_files(self):
        keys = (self._key("a.json"), self._key("b.json"))
        captured: list = []
        real_lock = store.postgres_lock_keys

        def spy(conn, names):
            captured.append(tuple(names))
            return real_lock(conn, names)

        async def main():
            async def fn(tx):
                tx.set(keys[0], {"n": 1})
                return "ok"

            return await store.transaction(keys, fn)

        with mock.patch.object(store, "postgres_lock_keys", spy):
            self.assertEqual(asyncio.run(main()), "ok")

        # store.transaction zámečky řadí vzestupně (prevence deadlocku).
        self.assertEqual(captured, [tuple(sorted(keys))])

    def test_callback_exception_writes_nothing_and_leaves_no_stuck_lock(self):
        k1 = self._key("a.json")
        self._seed(k1, {"n": 0})
        boom = RuntimeError("selhání testerovy operace")

        async def failing():
            async def fn(tx):
                tx.set(k1, {"n": 1})
                raise boom

            return await store.transaction((k1,), fn)

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(failing())
        self.assertIs(ctx.exception, boom, "výjimka musí letět dál v nezměněné podobě")
        self.assertEqual(self._raw(k1), {"n": 0}, "nic se nesmí zapsat")

        # wait_for převede případný deadlock na TimeoutError místo zavěšení CI.
        async def retry():
            async def fn(tx):
                tx.set(k1, {"n": 2})
                return "ok"

            return await asyncio.wait_for(
                store.transaction((k1,), fn), timeout=_TIMEOUT
            )

        self.assertEqual(asyncio.run(retry()), "ok")
        self.assertEqual(self._raw(k1), {"n": 2})

    def test_reads_are_strict_and_never_overwrite_on_read_failure(self):
        """JSONB je vždy validní JSON, takže poškozený vstup se simuluje výjimkou čtení."""
        k1 = self._key("a.json")
        self._seed(k1, {"n": 0})
        seen: dict = {}

        def spy(conn, file, default=None, *, strict=False):
            seen[file] = strict
            raise storage.DataCorruptionError(f"simulovaná korupce {file}")

        async def main():
            async def fn(tx):
                tx.get(k1, {})  # čtení je lazy – bez něj by postgres_save nikdy neselhala
                tx.set(k1, {"n": 999})
                return "neprobe"

            return await store.transaction((k1,), fn)

        with mock.patch.object(store, "postgres_load", spy):
            with self.assertRaises(storage.DataCorruptionError):
                asyncio.run(main())

        self.assertIs(
            seen.get(k1), True, "store.transaction musí číst přes strict=True"
        )
        self.assertEqual(self._raw(k1), {"n": 0}, "záznam nesmí být přepsán")

    def test_get_outside_declared_files_raises_value_error(self):
        covered = self._key("a.json")
        outside = self._key("b.json")

        async def main():
            async def fn(tx):
                with self.assertRaises(ValueError):
                    tx.get(outside, {})
                with self.assertRaises(ValueError):
                    tx.set(outside, {})
                return "checked"

            return await store.transaction((covered,), fn)

        self.assertEqual(asyncio.run(main()), "checked")
        self.assertFalse(self._exists(outside))

    def test_transaction_supports_sync_and_async_callbacks(self):
        k1 = self._key("sync.json")
        k2 = self._key("async.json")

        async def main():
            def sync_fn(tx):
                tx.set(k1, {"kind": "sync"})
                return "sync-done"

            async def async_fn(tx):
                tx.set(k2, {"kind": "async"})
                return "async-done"

            first = await store.transaction((k1,), sync_fn)
            second = await store.transaction((k2,), async_fn)
            return first, second

        self.assertEqual(asyncio.run(main()), ("sync-done", "async-done"))
        self.assertEqual(self._raw(k1), {"kind": "sync"})
        self.assertEqual(self._raw(k2), {"kind": "async"})

    def test_untouched_file_is_not_written(self):
        k1 = self._key("read.json")
        k2 = self._key("written.json")

        async def main():
            async def fn(tx):
                self.assertEqual(tx.get(k1, {"default": True}), {"default": True})
                tx.set(k2, {"n": 1})
                return "ok"

            return await store.transaction((k1, k2), fn)

        self.assertEqual(asyncio.run(main()), "ok")
        self.assertFalse(self._exists(k1), "soubor pouze přečtený nesmí vzniknout")
        self.assertEqual(self._raw(k2), {"n": 1})

    def test_schema_uses_existing_jsonb_layout(self):
        with storage.postgres_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """,
                (storage._POSTGRES_TABLE,),
            )
            columns = cur.fetchall()

        self.assertEqual(
            columns,
            [
                ("key", "text"),
                ("value", "jsonb"),
                ("updated_at", "timestamp with time zone"),
            ],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

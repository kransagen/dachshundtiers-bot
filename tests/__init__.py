"""Balíček testů – sdílené pomůcky.

Proč tu je ``json_backend_only``
--------------------------------
Většina testů staví fixture tak, že přesměruje ``storage.DATA_DIR`` do
tempdiru a píše JSON soubory přes ``storage.save_data``. Pod ``DATABASE_URL``
se ale ``DATA_DIR`` ignoruje a ``load_data``/``save_data`` jedou do
PostgreSQL – fixture by se tiše uložila do sdílené tabulky, kde ji vidí
i ostatní testy. Testy pak padají kvůli cizím datům (``9 != 2`` záznamů
v audit logu, 5 nálezů místo 1, …), nikoli kvůli chybě v kódu. Takový běh
není platným signálem, ať už je chyba reálná nebo ne.

Protože je ale JSON backend legitimní backend, který se má testovat, těmto
třídám se nepřeskakuje „nějak“ – jen se jim v setUp řekne, že vyžadují
JSON backend, a vypíše se proč. Backend se vyhodnocuje za běhu (``storage.
using_postgres()``), ne při importu, aby ho nešlo obejít patchem později.

Třídy, které ``storage.load_data``/``save_data`` mockují (nebo nečtou stav
vůbec), se NEOZNAČUJÍ – pod ``DATABASE_URL`` běží dál a chytají regrese.
Rozhodující je, zda test potřebuje DATA_DIR, ne to, že ho někde v ``setUp``
přesměrovává.

Proti ``DATABASE_URL`` se dá celý balík spustit dvěma způsoby:

* ``python -m unittest discover -s tests`` – JSON suite, ``test_store_postgres``
  se přeskočí,
* ``python -m unittest discover -s tests`` s ``DATABASE_URL`` +
  ``REQUIRE_POSTGRES_TESTS=1`` – JSON třídy se přeskočí, ``test_store_postgres``
  spustí a bez proměnné selže hlasitě (viz ``PostgresRequiredTests``).
"""

import functools

import storage

#: Zkrácená hláška pro přeskočení – ``reason`` z doporučení se připojí za ni.
_SKIP_PREFIX = "JSON backend test – s DATABASE_URL nemá smysl ("


def json_backend_only(reason: str):
    """Označí testovací třídu jako vyžadující JSON backend.

    :param reason: konkrétní důvod, proč třída potřebuje ``storage.DATA_DIR``
        a JSON soubory. Zobrazí se ve zprávě o přeskočení, aby bylo poznat,
        že nejde o obecné utlumení.
    """

    def decorate(cls):
        # ``cls.setUp`` (ne ``cls.__dict__``) rozliší i zděděnou setUp, takže
        # to volání funguje pro podtřády mixinu i pro třídy bez vlastní setUp.
        original_setUp = cls.setUp

        @functools.wraps(original_setUp)
        def setUp(self):
            if storage.using_postgres():
                self.skipTest(f"{_SKIP_PREFIX}{reason})")
            original_setUp(self)

        cls.setUp = setUp
        return cls

    return decorate

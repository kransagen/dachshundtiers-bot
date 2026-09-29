"""Balíček testů – sdílené pomůcky.

Proč tu je ``json_backend_only``
--------------------------------
``tests/conftest.py`` si pěstuje vlastní PostgreSQL (``embedded-postgres``)
pro DB vrstvu, ale ``DATABASE_URL`` **nenastavuje** – ``storage.using_postgres()``
tedy v testech vrací ``False`` a JSON backend se testuje v JSON režimu.

Jenže ``DATABASE_URL`` jde spustit i zvenčí (``DATABASE_URL=... pytest``).
Pak ``storage.load_data``/``save_data`` jedou do PostgreSQL, ``DATA_DIR`` se
ignoruje a fixture přesměrovaná do tempdiru se tiše uloží do sdílené tabulky,
kde ji uvidí i ostatní testy. Testy pak padají kvůli cizím datům, ne kvůli
chybě v kódu, a takový běh není platným signálem – ať už je chyba reálná
nebo ne. Těmto třídám se proto nepřeskakuje „nějak": vypíše se, co vyžadují.

Backend se vyhodnocuje **za běhu** (``storage.using_postgres()``), ne při
importu, aby ho nešlo obejít patchem až po načtení modulu.

Rozsah použití
--------------
Jen na třídy, jejichž fixture opravdu stojí na ``storage.DATA_DIR`` a na
zápisu JSON souborů. Třídy, které ``load_data``/``save_data`` mockují, se
NEOZNAČUJÍ – běží dál v obou backendech a chytají regrese. Rozhodující je,
zda test potřebuje DATA_DIR, ne to, že ho někde v setUp přesměrovává.

Poznámka k architektuře
------------------------
``origin/main`` JSON backend odstihl jako zdroj pravdy (``json_compat``
invarianty v ``services/phase_e/json_compat.py``). Tyto testy tedy NECHTĚJÍ
prosazovat JSON jako autoritu – chrání výhradně JSON kompatibilní vrstvu
(``storage.py`` + ``services/store.py``), kterou main stále živí.
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

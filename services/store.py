"""Async úložiště s per-souborovými zámky a transakcemi (bez discord.py).

Proč to existuje
-----------------
Bot si drží veškerý stav v JSON souborech (``data/*.json``). Původní kód dělal
``load_data`` → změna → ``save_data`` bez jakéhokoliv zámku, takže dvě souběžné
interakce (dva „Join", tester + hráč, dvě „Pull"...) se navzájem přepisovaly
(souboj o zápis → ztracený hráč / duplicitní join).

Tento modul serializuje přístup ke každému souboru přes ``asyncio.Lock`` a
nabízí atomické transakce přes více souborů („čti, zkontroluj, změň a ulož"
jako jeden kritický úsek).

Loop-scoped zámky
-----------------
``asyncio.Lock`` je vázaný na event loop, ve kterém vznikl – sdílení napříč
opakovanými ``asyncio.run()`` vyhodí „bound to a different event loop".
Registry proto drží zámky klíčované podle ``id(běžícího loopu)``, takže
funkce fungují jak v běžícím botovi (jeden loop), tak v testech.
"""

import asyncio
import logging

from storage import (
    commit_staged,
    data_path,
    discard_staged,
    load_data,
    postgres_connection,
    postgres_lock_keys,
    postgres_load,
    postgres_save,
    save_data,
    stage_data,
    using_postgres,
)

log = logging.getLogger("dachshundtiers")

# loop id -> {název souboru: asyncio.Lock}
_registry: "dict[int, dict[str, asyncio.Lock]]" = {}


def _locks() -> "dict[str, asyncio.Lock]":
    loop = asyncio.get_running_loop()
    key = id(loop)
    table = _registry.get(key)
    if table is None:
        table = {}
        _registry[key] = table
    return table


def _file_lock(name: str) -> asyncio.Lock:
    table = _locks()
    lock = table.get(name)
    if lock is None:
        lock = asyncio.Lock()
        table[name] = lock
    return lock


async def read(name: str, default=None):
    """Načte soubor pod zámkem (serializuje i čtení, nejen zápisy)."""
    async with _file_lock(name):
        return load_data(name, default)


async def write(name: str, data) -> None:
    """Uloží soubor pod zámkem."""
    async with _file_lock(name):
        save_data(name, data)


class Transaction:
    """Pracovní paměť transakce: líné čtení pod drženými zámky.

    - ``get(name, default)`` – načte soubor (jen jednou, pak z paměti),
    - ``set(name, data)``    – označí soubor k zápisu.
    """

    def __init__(self, names, postgres_conn=None):
        self._names = tuple(names)
        self._data = {}
        self._dirty = set()
        self._postgres_conn = postgres_conn

    def get(self, name: str, default=None):
        if name not in self._names:
            raise ValueError(f"Transakce nepokrývá soubor {name!r} (pokrývá: {self._names})")
        if name not in self._data:
            # strict=True zamezí přepsání poškozeného souboru odvozenými defaulty.
            if self._postgres_conn is not None:
                self._data[name] = postgres_load(
                    self._postgres_conn, name, default, strict=True
                )
            else:
                self._data[name] = load_data(name, default, strict=True)
        return self._data[name]

    def set(self, name: str, data) -> None:
        if name not in self._names:
            raise ValueError(f"Transakce nepokrývá soubor {name!r} (pokrývá: {self._names})")
        self._data[name] = data
        self._dirty.add(name)


async def _acquire(names):
    # Deterministické pořadí podle názvu → žádné deadlocky mezi transakcemi,
    # které berou různé podmnožiny souborů.
    locks = [_file_lock(name) for name in names]
    for lock in locks:
        await lock.acquire()
    return locks


def _commit_json(dirty, data) -> None:
    """Zapíše všechny změněné soubory JSON backendu dvoufázově.

    Fáze 1 – ``stage_data`` připraví KAŽDÝ změněný soubor jako dočasný
    soubor vedle cíle. Sem patří všechno, co může selhat kvůli datům nebo
    nedostatku místa (serializace do JSONu, zápis na disk, zakódování).

    Fázze 2 – teprve když jsou připravené všechny, ``commit_staged`` je
    přesune na místa přes ``os.replace``.

    Proč dvě fáze
    -------------
    Předchozí implementace volala ``save_data`` soubor po souboru, takže
    selhání až u třetího souboru nechalo první dva uložené navždy. Backend
    pak vypadal stejně jako PostgreSQL, ale choval se jinak – což je ta
    materialně odlišná sémantika, kterou F11 odstraňuje. Teď selhání při
    přípravě nemůže změnit ani jeden cílový soubor.

    Co atomické NENÍ (a být upřímný, tak to i je)
    --------------------------------------------
    Fáze 2 je stále N sdílených ``os.replace`` volání. POSIX nemá primitiv
    „přejmenuj N souborů najednou", takže plná vícesouborová atomicita je na
    běžném filesystemu nedosažitelná. Zbývá tedy úzké okno mezi jednotlivými
    přesuny: když selže až ``os.replace`` uprostřed (nebo když proces
    mezitím zabije SIGKILL), soubory nahrazené před tím zůstanou nové a
    zbývající staré.

    Oproti dřívějšímu stavu je to okno výrazně užší a mnohem méně
    pravděpodobné: zbývá jen série triviálních přesunů v řadě místo celého
    zápisu dat. Zaručeno je, že chyba *přípravy* dat už žádný cílový soubor
    nezmění. Vypnutí tohoto okna úplně by vyžadovalo přepsat JSON backend na
    databázový-style store, což je mimo rozsah F11.
    """
    staged = []
    # ``dirty`` je set, takže bez řazení by pořadí zápisů házelo s během
    # (hash seed) a selhání by bylo špatně reprodukovatelné. Zámečky se
    # berou ve stejném seřazeném pořadí, takže je to konzistentní i s nimi.
    ordered = sorted(dirty)
    try:
        for name in ordered:
            staged.append((stage_data(name, data[name]), data_path(name)))
    except Exception:
        # Nic nebylo přesunuto, takže cíle jsou nedotčené. Zbytky uklidíme.
        log.exception(
            "Příprava %d z %d souborů selhala; cílové soubory zůstaly beze změny.",
            len(staged),
            len(ordered),
        )
        discard_staged(staged)
        raise
    try:
        commit_staged(staged)
    except Exception:
        # Fáze 2 – viz omezení v dokumentaci. Zbývající dočasné soubory
        # uklidíme, ať nezůstanou v data/ jako směs.
        log.exception(
            "Nahrazení cílových souborů selhalo; transakce není atomická "
            "(část souborů už byla přepsána)."
        )
        discard_staged(staged)
        raise


async def transaction(names, fn):
    """Spustí ``fn(tx)`` atomicky přes dané soubory.

    - získá zámky veškerých souborů (seřazené podle názvu),
    - vytvoří :class:`Transaction` (líné čtení),
    - zavolá ``fn(tx)``, uloží změněné soubory a vrátí výsledek ``fn``,
    - chyba uvnitř ``fn`` → nic se neuloží, zámky se uvolní, výjimka letí dál.

    ``fn`` může být korutina i obyčejná funkce.

    Uložení změn je dvoufázové v obou backendech: nejprve se připraví všechna
    data, teprve potom se cokoli dotkne trvalého stavu. U PostgreSQL tou
    druhou fází je ``conn.commit()``; u JSON backendu je to série
    ``os.replace`` (viz :func:`_commit_json` pro přesné hranice toho, co
    atomicita zaručuje a co ne).
    """
    names = tuple(sorted(set(names)))
    locks = await _acquire(names)
    conn = None
    try:
        if using_postgres():
            # Jedna DB transakce pokryje všechny JSON-ekvivalentní záznamy;
            # DB tak nikdy neskončí se změněným players.json a nezměněným
            # cooldownem/ticketem po pádu uprostřed vícesouborové operace.
            conn_context = postgres_connection(autocommit=False)
            conn = conn_context.__enter__()
            postgres_lock_keys(conn, names)
            tx = Transaction(names, postgres_conn=conn)
        else:
            conn_context = None
            tx = Transaction(names)
        result = fn(tx)
        if asyncio.iscoroutine(result):
            result = await result
        if conn is not None:
            for name in tx._dirty:
                postgres_save(conn, name, tx._data[name])
            conn.commit()
        else:
            _commit_json(tx._dirty, tx._data)
        return result
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn_context.__exit__(None, None, None)
        for lock in reversed(locks):
            lock.release()

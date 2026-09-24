"""Pomocné funkce pro JSON nebo PostgreSQL úložiště stavu bota."""

import json
import logging
import os
from contextlib import contextmanager

log = logging.getLogger("dachshundtiers")

# Všechna data bota se ukládají do složky ./data vedle projektu
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Volitelný PostgreSQL backend. Když DATABASE_URL není nastavené, zachová se
# původní JSON úložiště, takže lokální vývoj a existující instalace fungují
# beze změny. Je-li ale URL nastavené, chyba databáze se NIKDY potichu
# nepřepne na JSON – vznikly by dva rozdílné zdroje pravdy.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_POSTGRES_SCHEMA_READY = False
_POSTGRES_TABLE = "dachshundtiers_data"


class DataCorruptionError(ValueError):
    """Poškozený nebo nečitelný JSON soubor.

    V transakcích (``services/store.Transaction``) se takový soubor NIKDY
    nepřepíše defaultními daty – operace se přeruší, chyba se zaloguje a
    původní soubor zůstane nedotčený (viz requirement „korupce dat se nikdy
    automaticky neopravuje přepsáním").
    """


def using_postgres() -> bool:
    """Je aktivní PostgreSQL backend?"""
    return bool(DATABASE_URL)


def backend_name() -> str:
    """Název aktivního backendu pro diagnostiku a startup log."""
    return "postgresql" if using_postgres() else "json"


def _psycopg():
    """Načte nepovinný driver až při skutečném použití PostgreSQL."""
    try:
        import psycopg
    except ImportError as err:
        raise RuntimeError(
            "DATABASE_URL je nastavené, ale chybí balíček psycopg. "
            "Spusť `pip install -r requirements.txt`."
        ) from err
    return psycopg


def _ensure_postgres_schema() -> None:
    """Vytvoří malé JSONB úložiště jednou za proces.

    Jednotlivé záznamy odpovídají dosavadním souborům (např. players.json),
    proto lze migraci zavést postupně bez přepisování celé business logiky.
    """
    global _POSTGRES_SCHEMA_READY
    if _POSTGRES_SCHEMA_READY:
        return
    psycopg = _psycopg()
    # Schéma vzniká ve své vlastní potvrzené transakci. Kdyby se vytvořilo
    # uvnitř následné business transakce a ta rollbackovala, process-level
    # příznak by omylem tvrdil, že tabulka dál existuje.
    with psycopg.connect(DATABASE_URL, autocommit=True) as schema_conn:
        with schema_conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_POSTGRES_TABLE} (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
    _POSTGRES_SCHEMA_READY = True


@contextmanager
def postgres_connection(*, autocommit: bool = True):
    """Otevře PostgreSQL spojení a zajistí existenci tabulky.

    Neexportuje se jako běžná aplikační cesta; používá jej i migrátor. Při
    transakci nastav ``autocommit=False`` a commit/rollback řídí volající.
    """
    if not using_postgres():
        raise RuntimeError("PostgreSQL není aktivní (nastav DATABASE_URL).")
    psycopg = _psycopg()
    try:
        _ensure_postgres_schema()
        with psycopg.connect(DATABASE_URL, autocommit=autocommit) as conn:
            yield conn
    except Exception as err:
        raise RuntimeError(f"PostgreSQL úložiště není dostupné: {err}") from err


def data_exists(file: str) -> bool:
    """Existuje záznam v aktivním úložišti?"""
    if not using_postgres():
        return os.path.exists(data_path(file))
    with postgres_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {_POSTGRES_TABLE} WHERE key = %s", (file,))
        return cur.fetchone() is not None


def postgres_load(conn, file: str, default=None, *, strict: bool = False):
    """Načte záznam přes již otevřené PostgreSQL spojení."""
    if default is None:
        default = []
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT value FROM {_POSTGRES_TABLE} WHERE key = %s", (file,))
            row = cur.fetchone()
        return default if row is None else row[0]
    except Exception as err:
        if strict:
            raise DataCorruptionError(
                f"Nelze načíst PostgreSQL záznam {file}: {err}"
            ) from err
        log.exception("Nelze načíst %s z PostgreSQL.", file)
        return default


def postgres_save(conn, file: str, data) -> None:
    """Zapíše záznam přes již otevřené PostgreSQL spojení."""
    psycopg = _psycopg()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {_POSTGRES_TABLE} (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (file, psycopg.types.json.Jsonb(data)),
        )


def postgres_lock_keys(conn, names) -> None:
    """Získá transakční advisory locky pro datové klíče.

    ``asyncio.Lock`` v services.store chrání jen jednu instanci bota. Tyto
    locky rozšíří stejnou garanci i na další procesy připojené ke stejné DB;
    automaticky se uvolní při commit/rollbacku.
    """
    with conn.cursor() as cur:
        for name in sorted(set(names)):
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(name),))


def ensure_data_dir() -> None:
    if using_postgres():
        with postgres_connection():
            pass
        return
    os.makedirs(DATA_DIR, exist_ok=True)


def data_path(file: str) -> str:
    return os.path.join(DATA_DIR, file)


def load_data(file: str, default=None, *, strict: bool = False):
    """Načte záznam z aktivního úložiště.

    Pokud soubor neexistuje, vrátí ``default`` (výchozí ``[]``, stejně jako
    v originále). Poškozený JSON / chyba čtení se zalogují (aby se problém
    neztrácel) a vrátí se ``default`` – POKUD není ``strict=True``.

    ``strict=True`` (používají transakce) u poškozeného/nečitelného souboru
    hází :class:`DataCorruptionError` místo tiché náhrady defaultem – volající
    tak soubor nikdy omylem nepřepíše nově odvozenými daty.
    """
    if default is None:
        default = []
    if using_postgres():
        try:
            with postgres_connection() as conn:
                return postgres_load(conn, file, default, strict=strict)
        except RuntimeError:
            if strict:
                raise
            log.exception("Nelze načíst %s z PostgreSQL.", file)
            return default

    ensure_data_dir()
    path = data_path(file)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        log.exception("Poškozený JSON soubor %s (%s) – použita výchozí hodnota.", path, err)
        if strict:
            raise DataCorruptionError(
                f"Poškozený JSON soubor {path} ({err})."
            ) from err
        return default
    except OSError as err:
        log.exception("Nelze přečíst %s: %s", path, err)
        if strict:
            raise DataCorruptionError(f"Nelze přečíst {path}: {err}") from err
        return default


def save_data(file: str, data) -> None:
    """Uloží data do aktivního úložiště atomicky.

    Nejprve se píše do dočasného souboru ve stejné složce a pak se přesune
    přes ``os.replace`` – při pádu uprostřed zápisu tak původní soubor
    zůstane nedotčený (žádný napůl zapsaný JSON).

    Při chybě zaloguje a výjimku posílá dál, aby se volající o selhání dozvěděl
    (ne „tichá ztráta dat").
    """
    if using_postgres():
        try:
            with postgres_connection() as conn:
                postgres_save(conn, file, data)
            return
        except Exception:
            log.exception("Zápis %s do PostgreSQL selhal.", file)
            raise

    ensure_data_dir()
    path = data_path(file)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        log.exception("Zápis souboru %s selhal; původní soubor zůstal nedotčen.", path)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise

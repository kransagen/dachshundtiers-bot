"""Pomocné funkce pro JSON nebo PostgreSQL úložiště stavu bota."""

import json
import logging
import os
import socket
from contextlib import contextmanager
from urllib.parse import urlparse

from db.config import _database_url_from_environment

log = logging.getLogger("dachshundtiers")

# Všechna data bota se ukládají do složky ./data vedle projektu
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Volitelný PostgreSQL backend. Když DATABASE_URL není nastavené, zachová se
# původní JSON úložiště, takže lokální vývoj a existující instalace fungují
# beze změny. Je-li ale URL nastavené, chyba databáze se NIKDY potichu
# nepřepne na JSON – vznikly by dva rozdílné zdroje pravdy.
DATABASE_URL = _database_url_from_environment()
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


def database_status() -> dict:
    """Read-only kontrola aktivního úložiště bez vyzrazení přihlašovacích údajů.

    Při PostgreSQL zároveň ověří, že tabulku dokáže vytvořit/číst. Je tedy
    vhodná pro admin diagnostiku po nastavení DB_HOST/DB_PASSWORD.
    """
    if not using_postgres():
        return {
            "backend": "json",
            "ok": True,
            "records": None,
            "message": "Používá se lokální JSON úložiště (DATABASE_URL/DB_* nejsou nastavené).",
        }
    try:
        with postgres_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_POSTGRES_TABLE}")
            records = int(cur.fetchone()[0])
        return {
            "backend": "postgresql",
            "ok": True,
            "records": records,
            "message": "PostgreSQL je dostupná a tabulka je připravená.",
        }
    except Exception:  # noqa: BLE001 – do Discordu neposíláme detail spojení
        log.exception("Kontrola PostgreSQL spojení selhala.")
        return {
            "backend": "postgresql",
            "ok": False,
            "records": None,
            "message": "PostgreSQL není dostupná; podrobnosti jsou v logu bota.",
        }


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


def _postgres_host() -> str:
    """Host z DB_HOST nebo DATABASE_URL; nikdy se nevypisuje do Discordu."""
    configured = os.getenv("DB_HOST", "").strip()
    if configured:
        return configured
    try:
        return urlparse(DATABASE_URL).hostname or ""
    except ValueError:
        return ""


def _ipv4_hostaddr() -> str:
    """Najde IPv4 pro DB host, když Docker/hosting nemá IPv6 konektivitu."""
    explicit = os.getenv("DB_HOSTADDR", "").strip()
    if explicit:
        return explicit
    host = _postgres_host()
    if not host:
        return ""
    try:
        addresses = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return ""
    return addresses[0][4][0] if addresses else ""


def _connect_postgres(psycopg, *, autocommit: bool):
    """Připojí PostgreSQL a při nedostupném IPv6 jednou zkusí IPv4."""
    try:
        return psycopg.connect(DATABASE_URL, autocommit=autocommit)
    except psycopg.OperationalError:
        hostaddr = _ipv4_hostaddr()
        if not hostaddr:
            raise
        log.warning("PostgreSQL přes výchozí adresu selhala; zkouším IPv4 fallback.")
        return psycopg.connect(
            DATABASE_URL, autocommit=autocommit, hostaddr=hostaddr
        )


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
    with _connect_postgres(psycopg, autocommit=True) as schema_conn:
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
        conn = _connect_postgres(psycopg, autocommit=autocommit)
    except Exception as err:
        raise RuntimeError(f"PostgreSQL úložiště není dostupné: {err}") from err
    with conn:
        yield conn


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


def _staged_path(path: str) -> str:
    """Cesta dočasného souboru, který později nahradí ``path``."""
    return f"{path}.{os.getpid()}.tmp"


def stage_data(file: str, data) -> str:
    """Připraví zápis do dočasného souboru; cílový soubor se NEMĚNÍ.

    Vrátí cestu k dočasnému souboru, který se buď přesune na místo přes
    :func:`commit_staged`, nebo se zahodí přes :func:`discard_staged`.

    Jde o tentýž zápis jako v :func:`save_data` (stejné kódování UTF-8,
    ``ensure_ascii=False``, dvouměrové odsazení), jen oddělený od
    ``os.replace``. Oddělení umožňuje vícesouborové transakci připravit
    všechny soubory dřív, než se částečně dotkne cílů (viz
    ``services.store.transaction``).

    Je určen pouze pro JSON backend; u PostgreSQL data zajišťuje transakce
    databáze.

    POZNÁMKA k právům: dočasný soubor vzniká s právy podle umask, stejně
    dřív, a ``os.replace`` je na cíl přenese. PRÁVA PŘEEXISTUJÍCÍHO souboru
    se tedy stejně jako před F11 neuchovávají – chování je záměrně
    nezměněné, ne zlepšené (F11 nemění nic mimo pořadí fází). Nechat by se
    to dalo přes ``os.chmod`` až po ``os.replace``, ale to by zavedlo vlastní
    okno, kde je soubor už nový a ještě bez původních práv. Viz
    ``tests/test_store_json_atomicity.py``, které pevně dokumentuje
    současné chování.

    Pokud serializace selhne napůl (``json.dump`` píše postupně, takže se
    dočasný soubor stihne částečně naplnit), dočasný soubor se tady smaže.
    Caller ho totiž do seznamu ``staged`` přidá až po úspěšném návratu, takže
    by ho jeho vlastní úklid už nezachytil.
    """
    ensure_data_dir()
    path = data_path(file)
    tmp_path = _staged_path(path)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path


def commit_staged(staged) -> None:
    """Přesune připravené dočasné soubory na jejich cíle (``os.replace``).

    ``staged`` je posloupnost dvojic ``(tmp_path, cílová_cesta)``.

    POZOR – tady je přesně ta hranice, za kterou už JSON backend není
    vícesouborově atomický. Každý ``os.replace`` je atomický *sám o sobě*,
    více přesunů za sebou ale POSIX neumí udělat v jednom kroku. Selhne-li
    ``os.replace`` uprostřed, soubory nahrazené před tím zůstanou nové.
    Proto se všechno náročné (serializace do JSONu, zápis na disk) dělá už
    v :func:`stage_data` a sem se dostane jen series triviálních přesunů.
    """
    for tmp_path, path in staged:
        os.replace(tmp_path, path)


def discard_staged(staged) -> None:
    """Smaže dočasné soubory, které se nestaly cílem (úspěch i neúspěch).

    Po úspěšném :func:`commit_staged` je bez účinku – soubory už byly
    přesunuty. Nepodařené smazání se zaloguje a nezruší běh, protože zaplněný
    disk je horší než zapomenutý dočasný soubor.
    """
    for tmp_path, _path in staged:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            log.warning("Dočasný soubor %s se nepodařilo smazat.", tmp_path)


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
    staged = []
    try:
        staged.append((stage_data(file, data), path))
        commit_staged(staged)
    except Exception:
        log.exception("Zápis souboru %s selhal; původní soubor zůstal nedotčen.", path)
        discard_staged(staged)
        raise

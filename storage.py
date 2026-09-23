"""Pomocné funkce pro načítání a ukládání dat (JSON databáze bota)."""

import json
import logging
import os

log = logging.getLogger("dachshundtiers")

# Všechna data bota se ukládají do složky ./data vedle projektu
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def data_path(file: str) -> str:
    return os.path.join(DATA_DIR, file)


def load_data(file: str, default=None):
    """Načte JSON soubor ze složky ``./data``.

    Pokud soubor neexistuje, vrátí ``default`` (výchozí ``[]``, stejně jako
    v originále). Poškozený JSON / chyba čtení se zalogují (aby se problém
    neztrácel) a vrátí se ``default``.
    """
    if default is None:
        default = []
    ensure_data_dir()
    path = data_path(file)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        log.exception("Poškozený JSON soubor %s (%s) – použita výchozí hodnota.", path, err)
        return default
    except OSError as err:
        log.exception("Nelze přečíst %s: %s", path, err)
        return default


def save_data(file: str, data) -> None:
    """Uloží data do JSON souboru ve složce ``./data`` ATOMICky.

    Nejprve se píše do dočasného souboru ve stejné složce a pak se přesune
    přes ``os.replace`` – při pádu uprostřed zápisu tak původní soubor
    zůstane nedotčený (žádný napůl zapsaný JSON).

    Při chybě zaloguje a výjimku posílá dál, aby se volající o selhání dozvěděl
    (ne „tichá ztráta dat").
    """
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

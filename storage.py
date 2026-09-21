"""Pomocné funkce pro načítání a ukládání dat (JSON databáze bota)."""

import json
import os

# Všechna data bota se ukládají do složky ./data vedle projektu
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def data_path(file: str) -> str:
    return os.path.join(DATA_DIR, file)


def load_data(file: str, default=None):
    """Načte JSON soubor ze složky ``./data``.

    Pokud soubor neexistuje nebo je poškozený, vrátí ``default``
    (výchozí ``[]``, stejně jako v originále).
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
    except (json.JSONDecodeError, OSError):
        return default


def save_data(file: str, data) -> None:
    """Uloží data do JSON souboru ve složce ``./data``."""
    ensure_data_dir()
    with open(data_path(file), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
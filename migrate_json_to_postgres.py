"""Jednorázová migrace JSON úložiště DACHSHUNDTIERS do PostgreSQL.

Použití:
    DATABASE_URL='postgresql://...' python migrate_json_to_postgres.py

Skript pouze přidává/aktualizuje záznamy z ``data/*.json``. Původní JSON
soubory nemaže; po migraci je proto možné nejdřív ověřit data a teprve pak
spustit bota s ``DATABASE_URL``.
"""

import json
import sys
from pathlib import Path

# Skript se spouští samostatně (bez bot.py/config.py), proto musí .env načíst
# sám. Jinak by viděl jen systémové env proměnné a chybně hlásil, že
# DATABASE_URL / DB_HOST nejsou nastavené.
from dotenv import load_dotenv

load_dotenv()

import storage  # noqa: E402 – musí být až po load_dotenv()


def main() -> int:
    if not storage.using_postgres():
        print("Chybí DATABASE_URL; migrace se nespustila.", file=sys.stderr)
        return 2

    data_dir = Path(storage.DATA_DIR)
    files = sorted(data_dir.glob("*.json"))
    if not files:
        print(f"Ve složce {data_dir} nejsou žádné JSON soubory.", file=sys.stderr)
        return 1

    migrated = 0
    for path in files:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as err:
            print(f"Přeskakuji {path.name}: neplatný/nečitelný JSON ({err}).", file=sys.stderr)
            continue
        storage.save_data(path.name, data)
        migrated += 1
        print(f"✓ {path.name}")

    print(f"Hotovo: migrováno {migrated} souborů do PostgreSQL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Drobné pomocné funkce sdílené napříč cogami."""

import os
from datetime import datetime

from config import TESTER_ROLE_FRAGMENT
from storage import data_path, load_data, save_data

# Výchozí sada kitů (použije se, dokud neexistuje data/kits.json)
DEFAULT_KITS = ["AnchorPvP", "NetheriteSword", "IronAxe", "GoldSMP", "UHCMace", "RandomPot"]


def get_kits():
    """Registrované kity z ``data/kits.json`` (nebo výchozí seznam)."""
    if not os.path.exists(data_path("kits.json")):
        return list(DEFAULT_KITS)
    raw = load_data("kits.json", [])
    return [k for k in raw if isinstance(k, str) and k.strip()] or []


def add_kit(kit: str) -> bool:
    """Přidá kit (case-insensitive podle názvu). Vrátí False, pokud už existuje."""
    kit = kit.strip()
    if not kit:
        return False
    kits = get_kits()
    if any(existing.lower() == kit.lower() for existing in kits):
        return False
    kits.append(kit)
    save_data("kits.json", kits)
    return True


def remove_kit(kit: str) -> bool:
    """Odebere kit. Vrátí False, pokud v seznamu nebyl."""
    kit = kit.strip().lower()
    if not kit:
        return False
    kits = get_kits()
    new_kits = [k for k in kits if k.lower() != kit]
    if len(new_kits) == len(kits):
        return False
    save_data("kits.json", new_kits)
    return True


def has_tester_role(member) -> bool:
    """Vrátí True, pokud má člen roli, jejíž název obsahuje „tester“."""
    if member is None:
        return False
    roles = getattr(member, "roles", None) or []
    return any(TESTER_ROLE_FRAGMENT in role.name.lower() for role in roles)


def today_cz() -> str:
    """Aktuální datum ve formátu DD.MM.YYYY (česky)."""
    return datetime.now().strftime("%d.%m.%Y")


def month_key() -> str:
    """Aktuální měsíc ve formátu MM.YYYY."""
    return datetime.now().strftime("%m.%Y")


def now_ms() -> int:
    """Aktuální čas v milisekundách (jako Date.now() v JS)."""
    return int(datetime.now().timestamp() * 1000)
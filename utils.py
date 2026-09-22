"""Drobné pomocné funkce sdílené napříč cogami."""

import os
from datetime import datetime

import discord
from discord import app_commands

from config import TESTER_ROLE_FRAGMENT
from storage import data_path, load_data, save_data

# Výchozí sada kitů (použije se, dokud neexistuje data/kits.json)
DEFAULT_KITS = [
    "AnchorPvP",
    "NetheriteSword",
    "IronAxe",
    "GoldSMP",
    "UHCMace",
    "RandomPot",
    "ShieldlessSMP",
]


def get_kits():
    """Registrované kity z ``data/kits.json`` (nebo výchozí seznam)."""
    if not os.path.exists(data_path("kits.json")):
        return list(DEFAULT_KITS)
    raw = load_data("kits.json", [])
    return [k for k in raw if isinstance(k, str) and k.strip()] or []


async def kit_autocomplete(
    interaction: discord.Interaction, current: str
):
    """Autocomplete názvů kitů pro všechny commandy s parametrem `kit`."""
    kits = get_kits() or list(DEFAULT_KITS)
    if current:
        kits = [k for k in kits if current.lower() in k.lower()]
    return [app_commands.Choice(name=k, value=k) for k in kits[:25]]


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


# --- „LT3 + eval" ---------------------------------------------------------
# Status mezi LT3 a HT3: hráč má pořád roli LT3, ale může otevírat HT3+
# tickety. Uchovává se v ``data/evals.json`` (host-local, NIKDY necommitovat):
#
#     {"uhcmace": {"hrac_ign": 1769550000000, ...}, ...}
#
EVALS_FILE = "evals.json"


def _eval_ign(ign: str) -> str:
    return (ign or "").strip().lower()


def get_evals() -> dict:
    raw = load_data(EVALS_FILE, {})
    return raw if isinstance(raw, dict) else {}


def has_eval(ign: str, kit: str) -> bool:
    """Má hráč (IGN) „LT3 + eval" pro daný kit?"""
    kit_key = (kit or "").strip().lower()
    bucket = get_evals().get(kit_key, {})
    return isinstance(bucket, dict) and _eval_ign(ign) in bucket


def set_eval(ign: str, kit: str) -> bool:
    """Nastaví hráči eval pro kit. Vrátí False při neplatném vstupu."""
    kit_key = (kit or "").strip().lower()
    key = _eval_ign(ign)
    if not key or not kit_key:
        return False
    evals = get_evals()
    evals.setdefault(kit_key, {})[key] = now_ms()
    save_data(EVALS_FILE, evals)
    return True


def unset_eval(ign: str, kit: str) -> bool:
    """Odebere hráči eval pro kit. Vrátí False, pokud ho neměl."""
    kit_key = (kit or "").strip().lower()
    evals = get_evals()
    bucket = evals.get(kit_key, {})
    if not isinstance(bucket, dict) or _eval_ign(ign) not in bucket:
        return False
    del bucket[_eval_ign(ign)]
    if not bucket:
        del evals[kit_key]
    save_data(EVALS_FILE, evals)
    return True
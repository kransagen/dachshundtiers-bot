"""Drobné pomocné funkce sdílené napříč cogami."""

from datetime import datetime

from config import TESTER_ROLE_FRAGMENT


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
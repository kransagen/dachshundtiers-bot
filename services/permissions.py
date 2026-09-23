"""Oprávnění testerů a adminů podle role (čistá logika, bez discord.py).

- ``has_tester_role``: když je nastaven allowlist ID rolí (``TESTER_ROLE_IDS``),
  tester se pozná POUZE podle přesného ID role. Bez allowlistu zůstává původní
  chování: role, jejíž název obsahuje ``TESTER_ROLE_FRAGMENT``.
- ``get_tester_roles``: najde tester ROLE v kolekci rolí (pro oprávnění
  kanálů ticketů) – stejná pravidla jako ``has_tester_role``.
- ``has_admin_role``: role z ``ADMIN_ROLE_IDS`` (když je allowlist nastaven)
  nebo Discord oprávnění Administrator. Bez allowlistu se chová přesně jako
  předtím (jen Administrator).
"""

from config import ADMIN_ROLE_IDS, TESTER_ROLE_FRAGMENT, TESTER_ROLE_IDS


def _has_role_id(member, role_ids) -> bool:
    """Má člen některou z rolí se zadaným ID?"""
    if not role_ids:
        return False
    allowed = set(role_ids)
    roles = getattr(member, "roles", None) or []
    for role in roles:
        rid = getattr(role, "id", None)
        if isinstance(rid, int) and rid in allowed:
            return True
    return False


def get_tester_roles(roles) -> list:
    """Vrátí role, které se považují za tester role (pro oprávnění kanálů).

    Při nastaveném allowlistu (``TESTER_ROLE_IDS``) přesná shoda ID, jinak
    původní chování: role, jejíž název obsahuje ``TESTER_ROLE_FRAGMENT``.
    Vstupem je libovolná kolekce role-objektů (``.id`` / ``.name``).
    """
    roles = list(roles or [])
    if TESTER_ROLE_IDS:
        allowed = set(TESTER_ROLE_IDS)
        return [
            r
            for r in roles
            if isinstance(getattr(r, "id", None), int) and r.id in allowed
        ]
    fragment = TESTER_ROLE_FRAGMENT
    return [
        r
        for r in roles
        if fragment in (getattr(r, "name", "") or "").lower()
    ]


def has_tester_role(member) -> bool:
    """Člen je tester: ID role v allowlistu, případně fragment v názvu role."""
    if member is None:
        return False
    if TESTER_ROLE_IDS:
        return _has_role_id(member, TESTER_ROLE_IDS)
    roles = getattr(member, "roles", None) or []
    return any(
        TESTER_ROLE_FRAGMENT in (getattr(role, "name", "") or "").lower()
        for role in roles
    )


def has_admin_role(member) -> bool:
    """Člen je admin: role z allowlistu (když je nastaven) nebo Administrator."""
    if member is None:
        return False
    if ADMIN_ROLE_IDS and _has_role_id(member, ADMIN_ROLE_IDS):
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(getattr(perms, "administrator", False))
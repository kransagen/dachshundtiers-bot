"""Testy role-based oprávnění services/permissions.py (bez discord.py)."""

import unittest
from types import SimpleNamespace
from unittest import mock

import services.permissions as permissions


def make_member(roles=None, administrator=False):
    member = SimpleNamespace()
    member.roles = roles or []
    member.guild_permissions = SimpleNamespace(administrator=administrator)
    return member


def role(rid, name):
    return SimpleNamespace(id=rid, name=name)


class PermissionsTests(unittest.TestCase):
    def test_tester_role_ids_allowlist_when_configured(self):
        """Přesný allowlist ID rolí má přednost před hledáním podle názvu."""
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", [101, 202]):
            m = make_member(roles=[role(101, "Tester"), role(303, "Admin")])
            self.assertTrue(permissions.has_tester_role(m))

            # role s „tester" v názvu, ale mimo allowlist → NENÍ tester
            m2 = make_member(roles=[role(404, "Moderátor tester skupiny")])
            self.assertFalse(permissions.has_tester_role(m2))

    def test_tester_fragment_fallback_when_no_ids(self):
        """Bez allowlistu se chová jako dřív (fragment v názvu role)."""
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", []):
            m = make_member(roles=[role(5, "Head Tester")])
            self.assertTrue(permissions.has_tester_role(m))
            m2 = make_member(roles=[role(6, "Moderátor")])
            self.assertFalse(permissions.has_tester_role(m2))

    def test_tester_fragment_from_config_is_case_insensitive(self):
        """Fragment z env může mít libovolný case, stejně jako název role."""
        with (
            mock.patch.object(permissions, "TESTER_ROLE_IDS", []),
            mock.patch.object(permissions, "TESTER_ROLE_FRAGMENT", "Tester"),
        ):
            member = make_member(roles=[role(5, "Head Tester")])
            self.assertTrue(permissions.has_tester_role(member))
            self.assertEqual(
                [r.id for r in permissions.get_tester_roles(member.roles)], [5]
            )

    def test_empty_tester_fragment_does_not_grant_every_role(self):
        with (
            mock.patch.object(permissions, "TESTER_ROLE_IDS", []),
            mock.patch.object(permissions, "TESTER_ROLE_FRAGMENT", ""),
        ):
            member = make_member(roles=[role(5, "Běžný člen")])
            self.assertFalse(permissions.has_tester_role(member))
            self.assertEqual(permissions.get_tester_roles(member.roles), [])

    def test_tester_empty_roles(self):
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", []):
            self.assertFalse(permissions.has_tester_role(make_member(roles=[])))

    def test_tester_none_member(self):
        self.assertFalse(permissions.has_tester_role(None))

    def test_admin_role_ids_allowlist(self):
        """ADMIN_ROLE_IDS dává přístup i bez oprávnění Administrator."""
        with mock.patch.object(permissions, "ADMIN_ROLE_IDS", [111]):
            m = make_member(roles=[role(111, "Vedení")], administrator=False)
            self.assertTrue(permissions.has_admin_role(m))

    def test_admin_fallback_administrator_permission(self):
        """Bez ADMIN_ROLE_IDS se chová jako dřív – jen Administrator permission."""
        with mock.patch.object(permissions, "ADMIN_ROLE_IDS", []):
            m = make_member(roles=[role(3, "cokoliv")], administrator=True)
            self.assertTrue(permissions.has_admin_role(m))
            m2 = make_member(roles=[role(3, "cokoliv")], administrator=False)
            self.assertFalse(permissions.has_admin_role(m2))

    def test_admin_none_member(self):
        self.assertFalse(permissions.has_admin_role(None))

    # ------------------------------------------------------------------
    # get_tester_roles – hledání tester ROLE pro oprávnění kanálů ticketů
    # ------------------------------------------------------------------
    def test_get_tester_roles_allowlist(self):
        roles = [role(101, "Tester"), role(202, "Tester/Admin"), role(303, "Moderátor")]
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", [101, 303]):
            found = permissions.get_tester_roles(roles)
            self.assertEqual([r.id for r in found], [101, 303])
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", []):
            self.assertEqual(permissions.get_tester_roles([]), [])

    def test_get_tester_roles_fragment_fallback(self):
        roles = [role(1, "Head Tester"), role(2, "Tester"), role(3, "Admin")]
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", []):
            found = permissions.get_tester_roles(roles)
            self.assertEqual([r.id for r in found], [1, 2])

    def test_get_tester_roles_none_or_empty(self):
        with mock.patch.object(permissions, "TESTER_ROLE_IDS", []):
            self.assertEqual(permissions.get_tester_roles(None), [])
            self.assertEqual(permissions.get_tester_roles([]), [])


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()
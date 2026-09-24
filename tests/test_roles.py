"""Testy automatických rolí kitů (cogs/roles.py).

Soustředí se na sdílené chování ``auto_grant_kit_role`` – volá se z
``/result`` i ``/topresult``, takže závady tady rozbíjejí oba flow.
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import storage
from cogs.roles import auto_grant_kit_role, KIT_ROLES_FILE


class AutoGrantKitRoleTests(unittest.TestCase):
    """Kit lookup je case-insensitive (klíče v kit_roles.json jsou lowercase)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        # /setkitrole ukládá klíče vždy lowercase – display-case názvy kitu
        # ("MolePVP") posílají volající; auto_grant_kit_role je sjednocuje.
        storage.save_data(KIT_ROLES_FILE, {"molepvp": {"HT3": "111"}})

    def _guild(self, member_roles=None):
        guild = mock.MagicMock()
        role = mock.MagicMock()
        role.mention = "<@&111>"
        role.id = 111
        guild.get_role.return_value = role
        member = mock.MagicMock()
        member.add_roles = mock.AsyncMock()
        member.remove_roles = mock.AsyncMock()
        member.roles = member_roles or []
        guild.get_member.return_value = member
        guild.fetch_member = mock.AsyncMock(return_value=member)
        return guild, member

    def test_display_case_kit_finds_lowercase_key(self):
        """Bývalý bug: "MolePVP" nenašel klíč "molepvp" → role se nedala."""
        async def main():
            guild, member = self._guild()
            note = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertIn("<@&111>", note)
            member.add_roles.assert_awaited_once()
        asyncio.run(main())

    def test_whitespace_kit_trimmed(self):
        async def main():
            guild, member = self._guild()
            note = await auto_grant_kit_role(guild, "1", "  MolePVP ", "HT3")
            self.assertIn("<@&111>", note)
            member.add_roles.assert_awaited_once()
        asyncio.run(main())

    def test_unknown_kit_returns_empty(self):
        async def main():
            guild, member = self._guild()
            note = await auto_grant_kit_role(guild, "1", "NopePvP", "HT3")
            self.assertEqual(note, "")
            member.add_roles.assert_not_awaited()
        asyncio.run(main())

    def test_unknown_tier_returns_hint_not_role(self):
        async def main():
            guild, member = self._guild()
            note = await auto_grant_kit_role(guild, "1", "MolePVP", "S")
            self.assertIn("nemáš namapovanou roli", note)
            member.add_roles.assert_not_awaited()
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
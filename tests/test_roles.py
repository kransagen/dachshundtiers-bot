"""Testy automatických rolí kitů (cogs/roles.py).

Soustředí se na sdílené chování ``auto_grant_kit_role`` – volá se z
``/result`` i ``/topresult``, takže závady tady rozbíjejí oba flow.
Protože je Discord jedinou autoritou aktuálních tierů, role se mění
JEDNÍM atomickým ``member.edit(roles=...)`` (žádné add_roles+remove_roles
zvlášť, žádný mezistav) a výsledek je strojově čitelný
(:class:`TierRoleGrant`) – ``ok=False`` znamená, že se role NEzměnila a
volající (PostgreSQL mirror) se tím řídí.
"""

import asyncio
import tempfile
import unittest
from unittest import mock

import discord
import storage
from cogs.roles import auto_grant_kit_role
from services.kit_roles import KIT_ROLES_FILE


def _role(rid, name=None):
    role = mock.MagicMock()
    role.id = rid
    role.mention = f"<@&{rid}>"
    role.name = name or f"Role{rid}"
    return role


class AutoGrantKitRoleTests(unittest.TestCase):
    """Kit lookup je case-insensitive (klíče v kit_roles.json jsou lowercase)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        # /setkitrole ukládá klíče vždy lowercase – display-case názvy kitu
        # ("MolePVP") posílají volající; auto_grant_kit_role je sjednocuje.
        storage.save_data(
            KIT_ROLES_FILE,
            {"molepvp": {"HT3": "111", "HT2": "222", "S": "333"}},
        )

    def _guild(self, member_roles=None):
        guild = mock.MagicMock()

        def get_role(rid):
            return _role(rid) if rid in (111, 222, 333) else None

        guild.get_role.side_effect = get_role
        member = mock.MagicMock()
        member.add_roles = mock.AsyncMock()
        member.remove_roles = mock.AsyncMock()
        member.edit = mock.AsyncMock()
        member.roles = member_roles or []
        guild.get_member.return_value = member
        guild.fetch_member = mock.AsyncMock(return_value=member)
        return guild, member

    def test_display_case_kit_finds_lowercase_key(self):
        """Bývalý bug: "MolePVP" nenašel klíč "molepvp" → role se nedala."""
        async def main():
            guild, member = self._guild()
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertIn("<@&111>", grant.note)
            self.assertEqual(grant.tier_role_id, 111)
            member.edit.assert_awaited_once()
            member.add_roles.assert_not_awaited()
            member.remove_roles.assert_not_awaited()
            _, kwargs = member.edit.await_args
            self.assertEqual({r.id for r in kwargs["roles"]}, {111})
        asyncio.run(main())

    def test_whitespace_kit_trimmed(self):
        async def main():
            guild, member = self._guild()
            grant = await auto_grant_kit_role(guild, "1", "  MolePVP ", "HT3")
            self.assertTrue(grant.ok)
            self.assertEqual(grant.tier_role_id, 111)
            member.edit.assert_awaited_once()
            member.add_roles.assert_not_awaited()
        asyncio.run(main())

    def test_unknown_kit_returns_not_ok(self):
        async def main():
            guild, member = self._guild()
            grant = await auto_grant_kit_role(guild, "1", "NopePvP", "HT3")
            self.assertFalse(grant.ok)
            member.edit.assert_not_awaited()
            member.add_roles.assert_not_awaited()
        asyncio.run(main())

    def test_unknown_tier_returns_hint_not_role(self):
        async def main():
            guild, member = self._guild()
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "X")
            self.assertFalse(grant.ok)
            self.assertIn("nemáš namapovanou roli", grant.note)
            member.edit.assert_not_awaited()
        asyncio.run(main())

    def test_other_kit_tiers_are_removed_in_one_edit(self):
        """Hráč má HT2 i HT3 – atomický edit nastaví jen HT3 (jeden call)."""
        async def main():
            guild, member = self._guild(member_roles=[_role(222), _role(333)])
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            member.edit.assert_awaited_once()
            _, kwargs = member.edit.await_args
            self.assertEqual({r.id for r in kwargs["roles"]}, {111})
        asyncio.run(main())

    def test_role_already_current_skips_edit(self):
        """Idempotence: role už je správná → žádné API volání, ok=True."""
        async def main():
            guild, member = self._guild(member_roles=[_role(111)])
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertEqual(grant.tier_role_id, 111)
            member.edit.assert_not_awaited()
            member.add_roles.assert_not_awaited()
            member.remove_roles.assert_not_awaited()
        asyncio.run(main())

    def test_forbidden_edit_reports_not_ok(self):
        async def main():
            guild, member = self._guild()
            member.edit.side_effect = discord.Forbidden(
                mock.Mock(status=403), "Forbidden"
            )
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertFalse(grant.ok)
            self.assertFalse(grant.ambiguous)
            self.assertIn("nepovedlo udělit", grant.note)
        asyncio.run(main())

    # ------------------------------------------------------------------
    # H6 audit fix: Discord client-side timeout/connection-error ambiguity.
    # The request may have actually succeeded server-side even though we
    # never got a confirmed response — the code must re-verify rather than
    # assume either outcome.
    # ------------------------------------------------------------------

    def test_timeout_then_verification_confirms_success(self):
        """Timeout on member.edit, but re-fetching the member shows the
        role change DID land — must be reported as a real success (ok=True),
        never as a plain failure just because the response was lost."""
        async def main():
            guild, member = self._guild()
            member.edit.side_effect = asyncio.TimeoutError("timed out")
            verified = mock.MagicMock()
            verified.roles = [_role(111)]
            guild.fetch_member = mock.AsyncMock(return_value=verified)
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertFalse(grant.ambiguous)
            self.assertEqual(grant.tier_role_id, 111)
        asyncio.run(main())

    def test_timeout_then_verification_confirms_failure(self):
        """Timeout on member.edit, re-fetch shows the role was NOT applied
        — a real, confirmed failure (ok=False), not ambiguous, since we
        successfully verified the actual state."""
        async def main():
            guild, member = self._guild()
            member.edit.side_effect = asyncio.TimeoutError("timed out")
            verified = mock.MagicMock()
            verified.roles = []  # role never landed
            guild.fetch_member = mock.AsyncMock(return_value=verified)
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertFalse(grant.ok)
            self.assertTrue(grant.ambiguous)
        asyncio.run(main())

    def test_timeout_then_verification_unavailable_is_ambiguous(self):
        """Timeout on member.edit AND re-fetch also fails — outcome is
        genuinely UNKNOWN. Must be reported distinctly (ambiguous=True), not
        silently folded into a plain ok=False failure, so callers never
        write a false PG mirror state or attempt a blind inverse fix."""
        async def main():
            guild, member = self._guild()
            member.edit.side_effect = asyncio.TimeoutError("timed out")
            guild.fetch_member = mock.AsyncMock(
                side_effect=discord.HTTPException(mock.Mock(status=503), "down")
            )
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertFalse(grant.ok)
            self.assertTrue(grant.ambiguous)
            self.assertIn("NEJISTÝ", grant.note)
        asyncio.run(main())

    def test_http_exception_ambiguous_is_reverified_like_timeout(self):
        """A generic discord.HTTPException (not Forbidden) is just as
        ambiguous as a timeout — must go through the same re-verify path."""
        async def main():
            guild, member = self._guild()
            member.edit.side_effect = discord.HTTPException(
                mock.Mock(status=502), "bad gateway"
            )
            verified = mock.MagicMock()
            verified.roles = [_role(111)]
            guild.fetch_member = mock.AsyncMock(return_value=verified)
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertFalse(grant.ambiguous)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
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
import unittest
from unittest import mock

import discord
import cogs.roles as roles_mod
from cogs.roles import TierRoleGrant, auto_grant_kit_role


def _role(rid, name=None):
    role = mock.MagicMock()
    role.id = rid
    role.mention = f"<@&{rid}>"
    role.name = name or f"Role{rid}"
    return role


def _fresh(member):
    """Member as a LIVE read would return it: current roles, fresh object."""
    snapshot = mock.MagicMock()
    snapshot.roles = list(member.roles)
    return snapshot


def _fresh_after(member, roles):
    """PATCH response carrying an UNUSABLE role list, but applying the edit.

    Mirrors a discord.py response object whose ``roles`` cannot be read as a
    concrete role-id set — the code must then re-read Discord live instead of
    trusting the body.
    """
    member.roles = list(roles)
    return mock.MagicMock(roles=mock.MagicMock())


class AutoGrantKitRoleTests(unittest.TestCase):
    """Kit lookup je case-insensitive (klíče v kit_roles jsou lowercase).

    ``auto_grant_kit_role``'s own concern is Discord mutation/confirmation
    semantics, not kit_roles persistence (that's covered by
    ``tests/test_services_kits_evals_roles_db.py`` against real PostgreSQL) —
    so the role-map lookup is mocked directly here rather than seeding a real
    database per test.
    """

    def setUp(self):
        # /setkitrole ukládá klíče vždy lowercase – display-case názvy kitu
        # ("MolePVP") posílají volající; auto_grant_kit_role je sjednocuje.
        roles_map = {"molepvp": {"HT3": 111, "HT2": 222, "S": 333}}

        async def _fake_get_kit_role_map(kit_key, *, session_factory=None):
            return dict(roles_map.get((kit_key or "").strip().lower(), {}))

        patch = mock.patch.object(
            roles_mod, "get_kit_role_map", side_effect=_fake_get_kit_role_map
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _guild(self, member_roles=None):
        guild = mock.MagicMock()

        def get_role(rid):
            return _role(rid) if rid in (111, 222, 333) else None

        guild.get_role.side_effect = get_role
        member = mock.MagicMock()
        member.add_roles = mock.AsyncMock()
        member.remove_roles = mock.AsyncMock()
        member.roles = member_roles or []
        guild.get_member.return_value = member

        # G0: `member.edit` and `guild.fetch_member` must behave like the real
        # discord.py — the PATCH response is the member AS DISCORD NOW SEES IT
        # (so a mock that returns an unrelated object would be confirming
        # nothing), and fetch_member returns a FRESH member reflecting the
        # applied state rather than the pre-edit cache.
        def _apply(roles):
            member.roles = list(roles)
            return member

        member.edit = mock.AsyncMock(side_effect=_apply)
        guild.fetch_member = mock.AsyncMock(side_effect=lambda _mid: _fresh(member))
        return guild, member

    def test_display_case_kit_finds_lowercase_key(self):
        """Bývalý bug: "MolePVP" nenašel klíč "molepvp" → role se nedala."""
        async def main():
            guild, member = self._guild()
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertTrue(grant.verified)
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
            self.assertTrue(grant.verified)
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
            self.assertTrue(grant.verified)
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
            self.assertTrue(grant.verified)
            self.assertEqual(grant.tier_role_id, 111)
            member.edit.assert_not_awaited()
            member.add_roles.assert_not_awaited()
            member.remove_roles.assert_not_awaited()
        asyncio.run(main())

    # ------------------------------------------------------------------
    # G0 (invariant 6): DISCORD STATE MUST BE VERIFIED before PostgreSQL
    # may mirror it. `ok` alone only means "the HTTP call did not raise" —
    # `verified` is the flag the canonical commit service actually requires,
    # and it defaults to False so a forgotten return site fails closed.
    # ------------------------------------------------------------------

    def test_default_verified_is_false(self):
        """Fail-closed by default: a hand-built grant that forgot to verify
        can never be mistaken for a confirmed Discord state."""
        self.assertFalse(TierRoleGrant(ok=True, tier_role_id=111).verified)

    def test_no_op_path_is_confirmed_by_live_read(self):
        """Role already matches the cache, but Discord is the authority —
        the cached match alone must NOT be reported as confirmed; the live
        re-read decides."""
        async def main():
            guild, member = self._guild(member_roles=[_role(111)])
            member.edit = mock.AsyncMock()
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertTrue(grant.verified)
            self.assertEqual(grant.tier_role_id, 111)
            member.edit.assert_not_awaited()
            guild.fetch_member.assert_awaited()
        asyncio.run(main())

    def test_no_op_path_unreadable_state_is_ambiguous_not_ok(self):
        """Cached roles match, but the LIVE state cannot be read at all.
        That is UNKNOWN, not 'unchanged' and not 'confirmed'."""
        async def main():
            guild, member = self._guild(member_roles=[_role(111)])
            guild.fetch_member = mock.AsyncMock(
                side_effect=discord.HTTPException(mock.Mock(status=503), "down")
            )
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertFalse(grant.ok)
            self.assertTrue(grant.ambiguous)
            self.assertFalse(grant.verified)
            self.assertIn("NEJISTÝ", grant.note)
        asyncio.run(main())

    def test_no_op_path_stale_cache_falls_through_to_mutation(self):
        """Cache says 'already HT3' but Discord says otherwise — the cache is
        stale, so the intended role set MUST be applied, not reported as a
        confirmed no-op."""
        async def main():
            guild, member = self._guild(member_roles=[_role(111)])
            stale = mock.MagicMock()
            stale.roles = []  # Discord disagrees with the cache
            guild.fetch_member = mock.AsyncMock(return_value=stale)
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertTrue(grant.verified)
            member.edit.assert_awaited_once()
            _, kwargs = member.edit.await_args
            self.assertEqual({r.id for r in kwargs["roles"]}, {111})
        asyncio.run(main())

    def test_success_path_uses_patch_response_as_confirmation(self):
        """A successful PATCH whose response body already shows the intended
        roles IS the confirmation — no extra API call needed."""
        async def main():
            guild, member = self._guild()
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertTrue(grant.verified)
            guild.fetch_member.assert_not_awaited()
        asyncio.run(main())

    def test_success_path_falls_back_to_live_read_when_body_unusable(self):
        """PATCH succeeded but its body does not describe roles → fall back
        to a live read, and only a matching read counts as confirmed."""
        async def main():
            guild, member = self._guild()
            member.edit = mock.AsyncMock(
                side_effect=lambda roles: _fresh_after(member, roles)
            )
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertTrue(grant.ok)
            self.assertTrue(grant.verified)
            guild.fetch_member.assert_awaited()
        asyncio.run(main())

    def test_success_path_unreadable_body_and_failing_read_is_ambiguous(self):
        """Edit did not raise, yet NOBODY can read the final state. The
        outcome is unknown — never a confirmed success (that would let PG
        mirror a state Discord never reported)."""
        async def main():
            guild, member = self._guild()
            member.edit = mock.AsyncMock(
                return_value=mock.MagicMock(roles=mock.MagicMock())
            )
            guild.fetch_member = mock.AsyncMock(
                side_effect=discord.HTTPException(mock.Mock(status=503), "down")
            )
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertFalse(grant.ok)
            self.assertTrue(grant.ambiguous)
            self.assertFalse(grant.verified)
            self.assertIn("NEJISTÝ", grant.note)
        asyncio.run(main())

    def test_success_path_confirmed_mismatch_is_not_ok(self):
        """Discord answered with a DIFFERENT role set than requested (e.g. a
        concurrent edit won the race). A confirmed non-application must be
        reported as failure, never as success — PG may not mirror it."""
        async def main():
            guild, member = self._guild()
            member.edit = mock.AsyncMock(
                side_effect=lambda roles: mock.MagicMock(roles=[_role(999)])
            )
            grant = await auto_grant_kit_role(guild, "1", "MolePVP", "HT3")
            self.assertFalse(grant.ok)
            self.assertFalse(grant.verified)
            self.assertFalse(grant.ambiguous)
            self.assertIn("jiný stav rolí", grant.note)
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
            self.assertFalse(grant.verified)
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
            self.assertTrue(grant.verified)
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
            self.assertFalse(grant.verified)
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
            self.assertFalse(grant.verified)
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
            self.assertTrue(grant.verified)
            self.assertFalse(grant.ambiguous)
        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
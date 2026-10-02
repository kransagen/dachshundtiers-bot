"""Testy sdílených Discord helperů cogs/_shared.py."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from cogs import _shared


class GuildMembersTests(unittest.TestCase):
    def test_fetch_failure_is_logged_and_cache_used(self):
        guild = mock.Mock(id=5)
        guild.members = [SimpleNamespace(bot=False), SimpleNamespace(bot=True)]
        guild.fetch_members.side_effect = RuntimeError("members intent off")
        with self.assertLogs("dachshundtiers", level="WARNING") as logs:
            members = asyncio.run(_shared.guild_members(guild))
        self.assertEqual(len(members), 1)
        self.assertIn("members intent off", logs.output[0])


class RoleReasonTests(unittest.TestCase):
    def _guild(self, member, role):
        guild = mock.Mock()
        guild.get_member.return_value = member
        guild.get_role.return_value = role
        return guild

    def test_apply_role_actions_passes_audit_log_reason(self):
        member = mock.Mock(roles=[])
        member.add_roles = mock.AsyncMock()
        member.remove_roles = mock.AsyncMock()
        guild = self._guild(member, object())
        actions = [
            {"op": "add", "member_id": 1, "role_id": 2},
            {"op": "remove", "member_id": 1, "role_id": 2},
        ]
        applied = asyncio.run(_shared.apply_role_actions(guild, actions))
        self.assertTrue(all(a["ok"] for a in applied))
        self.assertEqual(
            member.add_roles.await_args.kwargs["reason"], _shared.ROLE_ACTION_REASON
        )
        self.assertEqual(
            member.remove_roles.await_args.kwargs["reason"], _shared.ROLE_ACTION_REASON
        )

    def test_apply_rollback_actions_passes_audit_log_reason(self):
        member = mock.Mock(roles=[])
        member.add_roles = mock.AsyncMock()
        guild = self._guild(member, SimpleNamespace(id=2))
        asyncio.run(
            _shared.apply_rollback_actions(
                guild, [{"op": "add", "member_id": 1, "role_id": 2}]
            )
        )
        self.assertEqual(
            member.add_roles.await_args.kwargs["reason"], _shared.ROLLBACK_REASON
        )


if __name__ == "__main__":
    unittest.main()

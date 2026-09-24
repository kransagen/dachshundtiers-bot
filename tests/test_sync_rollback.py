"""Cog-level a executor testy rollbacku /sync discord.

Pokrývají požadavky safe-rollback specifikace na úrovni discord.py mocks:
- apply_rollback_actions: ADD-inverze zavolá remove_roles / REMOVE-inverze
  zavolá add_roles; idempotentní stavy (already_correct) bez volání API;
  člen/role nenalezen a Forbidden → failed (nikdy se nešíří dál),
- /sync discord-rollback: dry run nic nemění; apply → potvrzovací view;
  potvrzení provede inverzi a zapíše rollback audit; stale → nic,
- chybějící memberId/roleId v cílovém auditu → bezpečný abort,
- adminský gate u příkazu i u potvrzovacího tlačítka.

Mocks se neopírají o reálný Discord server.
"""

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
import storage
from cogs._shared import apply_rollback_actions
from cogs.sync import Sync, SyncDiscordRollbackView
from services import permissions
from services.playersync import (
    PLAYERSYNC_LOG_FILE,
    PLAYERSYNC_ROLLBACK_LOG_FILE,
    build_rollback_plan,
)

# ---------------------------------------------------------------------------
# Pomocné (stejný vzor jako test_sync.py, self-contained)
# ---------------------------------------------------------------------------


def _admin_member(rid: int = 999):
    return SimpleNamespace(
        id=999,
        roles=[SimpleNamespace(id=rid, name="Vedení")],
        guild_permissions=SimpleNamespace(administrator=False),
    )


def _plain_member():
    return SimpleNamespace(
        id=1,
        roles=[],
        guild_permissions=SimpleNamespace(administrator=False),
    )


def _interaction(user=None, guild=None):
    inter = mock.MagicMock()
    inter.user = user if user is not None else _plain_member()
    inter.guild = guild if guild is not None else mock.MagicMock()
    inter.response = mock.MagicMock()
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup = mock.MagicMock()
    inter.followup.send = mock.AsyncMock()
    inter.message = SimpleNamespace(edit=mock.AsyncMock())
    return inter


def _member(mid, name, role_ids=(), *, forbid_add=False, forbid_remove=False):
    m = SimpleNamespace(
        id=int(mid),
        name=name,
        nick=None,
        display_name=name,
        bot=False,
        roles=[SimpleNamespace(id=int(r)) for r in role_ids],
    )
    m.add_roles = mock.AsyncMock()
    m.remove_roles = mock.AsyncMock()
    if forbid_add:
        m.add_roles.side_effect = discord.Forbidden(mock.MagicMock(), "no perms")
    if forbid_remove:
        m.remove_roles.side_effect = discord.Forbidden(mock.MagicMock(), "no perms")
    return m


def _guild(members=()):
    by_id = {int(m.id): m for m in members}
    g = mock.MagicMock()
    g.members = list(members)
    g.get_member.side_effect = lambda mid: by_id.get(mid)
    g.fetch_member = mock.AsyncMock(side_effect=lambda mid: by_id.get(mid))
    g.get_role.side_effect = lambda rid: SimpleNamespace(
        id=int(rid), name=f"Role{rid}"
    )
    return g


def _sent(inter):
    args, kwargs = inter.followup.send.call_args
    if "embeds" in kwargs:
        embeds = kwargs["embeds"]
    elif "embed" in kwargs:
        embeds = [kwargs["embed"]]
    elif args:
        first = args[0]
        embeds = first if isinstance(first, list) else [first]
    else:
        embeds = []
    return (embeds[0] if embeds else None), kwargs


def _rec(op, member_id, role_id, *, ok=True, tier="HT3"):
    return {
        "op": op,
        "memberId": str(member_id),
        "memberName": "AliceMC",
        "roleId": str(role_id),
        "kit": "AnchorPvP",
        "tier": tier,
        "ok": ok,
        "error": None if ok else "forbidden",
    }


def _apply_entry(ts=1000, applied=None, actor="boss"):
    return {
        "ts": ts,
        "mode": "apply",
        "actorId": "999",
        "actorName": actor,
        "summary": {},
        "applied": applied or [_rec("add", "1", "101"), _rec("remove", "1", "202")],
    }


def _rollback_action(op, role_id, *, original_op=None):
    return {
        "op": op,
        "original_op": original_op or ("remove" if op == "add" else "add"),
        "member_id": "1",
        "member_name": "AliceMC",
        "role_id": str(role_id),
        "kit": "AnchorPvP",
        "tier": "HT3",
    }


# ---------------------------------------------------------------------------
# Executor: apply_rollback_actions (cogs/_shared)
# ---------------------------------------------------------------------------
class ApplyRollbackActionsTests(unittest.TestCase):
    def test_remove_original_inverts_to_add(self):
        alice = _member(1, "AliceMC")  # bez role 101
        guild = _guild([alice])
        actions = [_rollback_action("add", 101, original_op="remove")]

        async def main():
            await apply_rollback_actions(guild, actions)

        asyncio.run(main())
        alice.add_roles.assert_awaited_once()
        self.assertEqual(alice.add_roles.await_args.args[0].id, 101)
        alice.remove_roles.assert_not_awaited()

    def test_add_original_inverts_to_remove(self):
        alice = _member(1, "AliceMC", role_ids=(101,))
        guild = _guild([alice])
        actions = [_rollback_action("remove", 101, original_op="add")]

        async def main():
            await apply_rollback_actions(guild, actions)

        asyncio.run(main())
        alice.remove_roles.assert_awaited_once()
        self.assertEqual(alice.remove_roles.await_args.args[0].id, 101)
        alice.add_roles.assert_not_awaited()

    def test_already_present_role_during_add_is_idempotent(self):
        alice = _member(1, "AliceMC", role_ids=(101,))  # roli už drží
        guild = _guild([alice])
        actions = [_rollback_action("add", 101)]

        async def main():
            return await apply_rollback_actions(guild, actions)

        results = asyncio.run(main())
        alice.add_roles.assert_not_awaited()
        self.assertEqual(results[0]["status"], "already_correct")

    def test_already_absent_role_during_remove_is_idempotent(self):
        alice = _member(1, "AliceMC")  # roli nedrží
        guild = _guild([alice])
        actions = [_rollback_action("remove", 101)]

        async def main():
            return await apply_rollback_actions(guild, actions)

        results = asyncio.run(main())
        alice.remove_roles.assert_not_awaited()
        self.assertEqual(results[0]["status"], "already_correct")

    def test_partial_failure_continues_and_reports(self):
        # Alice drží roli 102, kterou má rollback odebrat (remove selže);
        # roli 101 nemá, ta se má přidat (proběhne).
        alice = _member(1, "AliceMC", role_ids=(102,), forbid_remove=True)
        guild = _guild([alice])
        actions = [
            _rollback_action("add", 101),          # ok
            _rollback_action("remove", 102),       # Forbidden
        ]

        async def main():
            return await apply_rollback_actions(guild, actions)

        results = asyncio.run(main())
        self.assertEqual(results[0]["status"], "applied")
        self.assertEqual(results[1]["status"], "failed")
        self.assertIn("no perms", results[1]["error"])

    def test_member_not_found_is_failure_not_idempotent(self):
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        actions = [dict(_rollback_action("add", 101), member_id="999")]

        async def main():
            return await apply_rollback_actions(guild, actions)

        results = asyncio.run(main())
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("člen není na serveru", results[0]["error"])

    def test_role_not_found_is_failure(self):
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        guild.get_role.side_effect = lambda rid: None
        actions = [_rollback_action("add", 101)]

        async def main():
            return await apply_rollback_actions(guild, actions)

        results = asyncio.run(main())
        self.assertEqual(results[0]["status"], "failed")
        self.assertIn("role neexistuje", results[0]["error"])

    def test_second_run_is_all_already_correct_no_corruption(self):
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        actions = [
            _rollback_action("add", 101),     # remove-inverze
            _rollback_action("remove", 909),  # add-inverze (909 nikdy nedržel)
        ]

        async def first():
            return await apply_rollback_actions(guild, actions)

        async def second():
            return await apply_rollback_actions(guild, actions)

        r1 = asyncio.run(first())
        self.assertEqual([r["status"] for r in r1], ["applied", "already_correct"])
        # simulace stavu po prvním běhu: Alice teď roli 101 drží
        alice.roles.append(SimpleNamespace(id=101))
        r2 = asyncio.run(second())
        self.assertEqual([r["status"] for r in r2], ["already_correct", "already_correct"])
        # žádné další volání API
        self.assertEqual(alice.add_roles.await_count, 1)
        self.assertEqual(alice.remove_roles.await_count, 0)


# ---------------------------------------------------------------------------
# Příkaz /sync discord-rollback (cogs/sync)
# ---------------------------------------------------------------------------
class SyncDiscordRollbackCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

    def _seed(self, entry=None):
        storage.save_data(
            PLAYERSYNC_LOG_FILE,
            [entry or _apply_entry()],
        )

    def test_dry_run_reports_target_and_changes_nothing(self):
        self._seed()
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "preview")

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("🔄 /sync discord-rollback", embed.title)
        self.assertIn("Dry run", embed.footer.text)
        self.assertIn("Celkem: **2**", embed.description)
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        # rollback preview audit v separátním souboru
        rollbacks = storage.load_data(PLAYERSYNC_ROLLBACK_LOG_FILE, [])
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(rollbacks[0]["mode"], "preview")

    def test_apply_shows_confirm_view_no_side_effects(self):
        self._seed()
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "apply")

        asyncio.run(main())
        embed, kwargs = _sent(inter)
        self.assertIsInstance(kwargs.get("view"), SyncDiscordRollbackView)
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()

    def test_confirm_executes_inverse_and_writes_audit(self):
        self._seed()
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")  # bez rolí
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            await view.confirm.callback(
                _interaction(user=_admin_member(), guild=guild)
            )

        asyncio.run(main())
        # add 202 (inverze remove 202) se aplikuje; remove 101 (inverze add 101)
        # je už správně (Alice roli 101 nedrží)
        alice.add_roles.assert_awaited_once()
        self.assertEqual(alice.add_roles.await_args.args[0].id, 202)
        alice.remove_roles.assert_not_awaited()
        rollbacks = storage.load_data(PLAYERSYNC_ROLLBACK_LOG_FILE, [])
        apply_entries = [e for e in rollbacks if e.get("mode") == "apply"]
        self.assertEqual(len(apply_entries), 1)
        statuses = [r["status"] for r in apply_entries[0]["results"]]
        self.assertEqual(statuses, ["already_correct", "applied"])
        # původní audit syncu zůstal nedotčený
        sync_audit = storage.load_data(PLAYERSYNC_LOG_FILE, [])
        self.assertEqual(len(sync_audit), 1)
        self.assertEqual(sync_audit[0]["mode"], "apply")

    def test_confirm_stale_audit_applies_nothing(self):
        self._seed()
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            holder["confirm"] = _interaction(user=_admin_member(), guild=guild)
            # audit se mezitím změnil
            storage.save_data(PLAYERSYNC_LOG_FILE, [])
            await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        embed, _ = _sent(holder["confirm"])
        self.assertIn("audit se změnil", embed.title)

    def test_confirm_reports_partial_failure(self):
        self._seed(
            _apply_entry(applied=[_rec("add", "1", "101"), _rec("remove", "1", "202")])
        )
        cog = Sync.__new__(Sync)
        # Alice drží roli 101 (přidanou syncu) a forbid_remove=True →
        # inverze add→remove(101) selže; inverze remove→add(202) aplikuje.
        alice = _member(1, "AliceMC", role_ids=(101,), forbid_remove=True)
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            holder["confirm"] = _interaction(user=_admin_member(), guild=guild)
            await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        embed, _ = _sent(holder["confirm"])
        self.assertIn("Aplikováno: **1** · Už správně: **0** · Chyby: **1**", embed.description)
        self.assertIn("no perms", embed.description)
        # audit zachytil chybu
        rollbacks = storage.load_data(PLAYERSYNC_ROLLBACK_LOG_FILE, [])
        apply_entries = [e for e in rollbacks if e.get("mode") == "apply"]
        self.assertEqual(apply_entries[0]["summary"]["failed"], 1)

    def test_missing_member_id_rejected(self):
        self._seed(
            _apply_entry(
                applied=[{"op": "add", "roleId": "101", "ok": True, "error": None}]
            )
        )
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "preview")

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("❌ /sync discord-rollback – chybí informace", embed.title)
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()

    def test_missing_role_id_rejected(self):
        self._seed(
            _apply_entry(
                applied=[{"op": "add", "memberId": "1", "ok": True, "error": None}]
            )
        )
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member(), guild=_guild([]))

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "preview")

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("❌ /sync discord-rollback – chybí informace", embed.title)

    def test_no_applied_sync_in_log(self):
        storage.save_data(PLAYERSYNC_LOG_FILE, [])
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member(), guild=_guild([]))

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "preview")

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("❌ /sync discord-rollback – nelze", embed.title)

    def test_permission_denied(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.sync_discord_rollback.callback(cog, inter, "preview")

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )

    def test_confirm_view_permission_denied(self):
        plan = build_rollback_plan(_apply_entry())
        view = SyncDiscordRollbackView(plan=plan)
        inter = _interaction(user=_plain_member())

        async def main():
            await view.confirm.callback(inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
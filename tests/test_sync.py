"""Cog-level testy centrální synchronizace – cogs/sync.py (a deprecated aliasy).

Pokrývají požadavky konsolidace /sync (orchestrace, logika zůstává ve
službách):

- ``/sync check``    – read-only diagnostika: severity OK/WARNING/CONFLICT/
                       ERROR, filter podle oblasti (area), web nedostupný se
                       NEPOSÍLÁ do analyze_websync (žádné falešné nálezy),
                       poškozená data → bezpečný abort, idempotence, audity
                       zůstávají ve stávajících checkweb_log/datacheck_log,
- ``/sync discord``  – preview nic nemění; apply zobrazí potvrzovací view;
                       potvrzení aplikuje ROLE (nikdy DB), stale → nic,
                       per-akce chyby (Forbidden) → PARTIAL, retired tiery se
                       nesynchronizují, po aplikaci idempotentní (žádné akce),
- ``/sync web``      – preview nic neposílá; apply → view; potvrzení nahraje
                       přes services.websync (GitHub), failure NIKDY není
                       success, bez GITHUB_TOKEN → nelze, prázdná DB se
                       neposílá, stale → nic,
- ``/sync data``     – report + bezpečné opravy po potvrzení (ticket se zavře,
                       záznam zůstává), poškozená data → abort,
- deprecated aliasy /playersync /websync /checkweb /datacheck delegují na
  sdílené orchestrace (`_run_discord`/`_run_web`/`_run_check`/`_run_data`/
  `_run_checkweb_apply`) a v patičce upozorní „Deprecated",
- administrátorský gate (has_admin_role / ADMIN_ROLE_IDS) u KAŽDÉ operace
  i u potvrzovacích tlačítek.
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
import github_sync
import storage
from services import permissions
from services.checkweb import CHECKWEB_LOG_FILE
from services.datacheck import DATACHECK_LOG_FILE
from services.playersync import PLAYERSYNC_LOG_FILE
from services.websync import WEBSYNC_LOG_FILE
from storage import DataCorruptionError

from cogs.sync import (
    Sync,
    SyncDataRepairView,
    SyncDiscordConfirmView,
    SyncWebConfirmView,
    _check_embed,
    _datacheck_embed,
    _send_embed_pack,
    build_embed_pack,
)


# ---------------------------------------------------------------------------
# Pomocné (stejný vzor jako test_edituser)
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
    """Discord member mock (čtení pro make_member + add_roles/remove_roles)."""
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


def _guild(members=(), *, existing_channels=()):
    by_id = {int(m.id): m for m in members}
    g = mock.MagicMock()
    g.members = list(members)
    g.get_member.side_effect = lambda mid: by_id.get(mid)
    g.fetch_member = mock.AsyncMock(side_effect=lambda mid: by_id.get(mid))
    g.get_role.side_effect = lambda rid: SimpleNamespace(
        id=int(rid), name=f"Role{rid}"
    )
    existing = set(existing_channels)

    def _channel(cid):
        try:
            return SimpleNamespace(id=int(cid)) if int(cid) in existing else None
        except (TypeError, ValueError):
            return None

    g.get_channel.side_effect = _channel
    return g


def _deep(data):
    return json.loads(json.dumps(data))


def _write_corrupt(name):
    """Přímo zapíše poškozený JSON (obchází save_data, které serializuje)."""
    path = os.path.join(storage.DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ definitely not json !!")


def _player(username, modes, discord_id=None, history=None):
    p = {"username": username, "modes": dict(modes)}
    if discord_id is not None:
        p["discordId"] = discord_id
    if history is not None:
        p["history"] = dict(history)
    return p


def _sent(inter):
    """(první embed, kwargs) z jediného followup.send volání.

    Podporuje ``embed=`` i ``embeds=`` (embed pack) – vrací PRVNÍ embed packu,
    takže staré testy (titulky, description, pole) fungují beze změny.
    """
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


# ---------------------------------------------------------------------------
# /sync check – read-only diagnostika
# ---------------------------------------------------------------------------
class SyncCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

        # Komplexní scénář – všechny severity najednou (ověřeno proti reálným
        # službám):
        #   conflict  = Alice (Discord HT3 vs DB HT2 -> DATABASE_MISMATCH)
        #   warning   = 4× MISSING_DISCORD_ROLE + 3× websync missing_player
        #               + invalid_tiers (Dave) + 2× missing_website_records
        #               + retired_tiers_in_modes (Carol)
        #   error     = duplicate_player_discord_ids (Alice+Eve)
        self.players = [
            _player("AliceMC", {"randompot": "HT2"}, discord_id="1",
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT2"}]}),
            _player("BobMC", {"randompot": "HT3"}, discord_id="2",
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
            _player("CarolMC", {"randompot": "RHT2"}, discord_id="3",
                    history={"randompot": [{"date": "01.01.2024", "tier": "HT3"}]}),
            _player("DaveMC", {"randompot": "NONSENSE"}, discord_id="4"),
            _player("EveMC", {"randompot": "HT2"}, discord_id="1"),
        ]
        self.web = [
            _player("AliceMC", {"randompot": "HT2"},
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT2"}]}),
            _player("BobMC", {"randompot": "HT3"},
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
        ]
        self.members = [
            _member(1, "AliceMC", [101]),  # 101 = HT3 → DATABASE_MISMATCH
            _member(2, "BobMC", []),
            _member(3, "CarolMC", []),
            _member(4, "DaveMC", []),
        ]
        storage.save_data("players.json", _deep(self.players))
        storage.save_data("kit_roles.json", {"randompot": {"HT3": "101", "HT2": "102"}})

    def _run(self, inter, area="all", *, web=(True,)):
        cog = Sync.__new__(Sync)

        async def main():
            if web:
                fetcher = mock.AsyncMock(return_value=(_deep(self.web), "sha", None))
            else:
                fetcher = mock.AsyncMock(return_value=(None, None, "rate limited"))
            with mock.patch.object(
                github_sync, "fetch_players", new=fetcher
            ), mock.patch(
                "cogs.sync.guild_members",
                new=mock.AsyncMock(return_value=list(self.members)),
            ):
                await Sync.sync_check.callback(cog, inter, area=area)

        asyncio.run(main())

    def test_check_read_only_aggregates_severities(self):
        inter = _interaction(user=_admin_member())
        self._run(inter)
        embed, kwargs = _sent(inter)
        self.assertEqual(kwargs.get("ephemeral"), True)
        self.assertIn("🔎 /sync check", embed.title)
        self.assertIn("❌ ERROR: **1**", embed.description)
        self.assertIn("🔶 CONFLICT: **1**", embed.description)
        self.assertIn("⚠️ WARNING: **11**", embed.description)
        self.assertIn("Nálezů celkem: **13**", embed.description)
        self.assertIn("**GitHub**", embed.description)
        field_names = [f.name for f in embed.fields]
        self.assertTrue(any("ERROR (1)" in n for n in field_names))
        self.assertTrue(any("CONFLICT (1)" in n for n in field_names))
        self.assertTrue(any("WARNING (11)" in n for n in field_names))
        # read-only – data beze změny
        self.assertEqual(storage.load_data("players.json", []), self.players)

    def test_check_writes_into_existing_audit_logs(self):
        inter = _interaction(user=_admin_member())
        self._run(inter)
        cw_log = storage.load_data(CHECKWEB_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "preview" for e in cw_log))
        dc_log = storage.load_data(DATACHECK_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "check" for e in dc_log))

    def test_check_area_filter_web(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, area="web")
        embed, _ = _sent(inter)
        self.assertIn("⚠️ WARNING: **5**", embed.description)  # 3 websync + 2 datacheck
        self.assertIn("❌ ERROR: **0**", embed.description)
        self.assertIn("🔶 CONFLICT: **0**", embed.description)
        self.assertIn("(Web)", embed.title)

    def test_check_area_filter_identity(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, area="identity")
        embed, _ = _sent(inter)
        self.assertIn("❌ ERROR: **1**", embed.description)
        self.assertIn("⚠️ WARNING: **0**", embed.description)

    def test_check_area_filter_roles(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, area="roles")
        embed, _ = _sent(inter)
        self.assertIn("🔶 CONFLICT: **1**", embed.description)
        self.assertIn("⚠️ WARNING: **4**", embed.description)

    def test_check_website_unavailable_skips_websync_no_false_positives(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, web=False)
        embed, _ = _sent(inter)
        self.assertIn("nedostupný", embed.description)
        # warning: 4 checkweb MISSING + 1 invalid + 2 missing_website +
        #          1 retired – ŽÁDNÝ websync missing_player (falešné nálezy)
        self.assertIn("⚠️ WARNING: **8**", embed.description)
        self.assertIn("❌ ERROR: **1**", embed.description)

    def test_check_corrupted_data_aborts_cleanly(self):
        _write_corrupt("players.json")
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())
        guild_members = mock.AsyncMock()

        async def main():
            with mock.patch("cogs.sync.guild_members", new=guild_members):
                await Sync.sync_check.callback(cog, inter)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("❌ /sync check – poškozená data", embed.title)
        self.assertIn("`players.json`", embed.description)
        # bezpečný abort – žádná analýza se neproběhla
        guild_members.assert_not_awaited()

    def test_check_idempotent(self):
        inter1 = _interaction(user=_admin_member())
        inter2 = _interaction(user=_admin_member())
        self._run(inter1)
        self._run(inter2)
        embed1, _ = _sent(inter1)
        embed2, _ = _sent(inter2)
        self.assertEqual(embed1.description, embed2.description)
        self.assertEqual(
            [f.name + f.value for f in embed1.fields],
            [f.name + f.value for f in embed2.fields],
        )

    def test_check_permission_denied(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.sync_check.callback(cog, inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()

    def test_check_denied_outside_guild(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())
        inter.guild = None

        async def main():
            await Sync.sync_check.callback(cog, inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze na serveru.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()


# ---------------------------------------------------------------------------
# /sync discord – DB → Discord role (RoleSyncService)
# ---------------------------------------------------------------------------
class SyncDiscordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

        self.players = [
            _player("AliceMC", {"randompot": "HT3"}, discord_id="1",
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
        ]
        storage.save_data("players.json", _deep(self.players))
        storage.save_data(
            "kit_roles.json", {"randompot": {"HT3": "101", "HT2": "102"}}
        )

    def test_preview_reports_but_changes_nothing(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(
            user=_admin_member(), guild=_guild([_member(1, "AliceMC")])
        )

        async def main():
            await Sync.sync_discord.callback(cog, inter, "preview")

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("🔎 /sync discord – náhled", embed.title)
        self.assertIn("Náhled – žádné změny neaplikovány", embed.footer.text)
        # audit preview se zapisuje, ale role žádná neproběhla
        audits = storage.load_data(PLAYERSYNC_LOG_FILE, [])
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[-1]["mode"], "preview")
        self.assertEqual(storage.load_data("players.json", []), self.players)

    def test_apply_shows_confirm_view_no_side_effects(self):
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)

        async def main():
            await Sync.sync_discord.callback(cog, inter, "apply")

        asyncio.run(main())
        embed, kwargs = _sent(inter)
        self.assertIn("potvrzení změn", embed.title)
        self.assertIsInstance(kwargs.get("view"), SyncDiscordConfirmView)
        alice.add_roles.assert_not_awaited()
        # žádné apply audity, dokud se nepotvrdí
        audits = storage.load_data(PLAYERSYNC_LOG_FILE, [])
        self.assertTrue(all(e.get("mode") != "apply" for e in audits))

    def test_confirm_applies_expected_actions_and_audits(self):
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)

        async def main():
            await Sync.sync_discord.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            await view.confirm.callback(_interaction(user=_admin_member(), guild=guild))

        asyncio.run(main())
        # přesně akce z analýzy – add ROLE 101 (nikdy zápis do DB)
        alice.add_roles.assert_awaited_once()
        role = alice.add_roles.await_args.args[0]
        self.assertEqual(role.id, 101)
        audits = storage.load_data(PLAYERSYNC_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "apply" for e in audits))
        self.assertEqual(storage.load_data("players.json", []), self.players)

    def test_confirm_stale_fingerprint_applies_nothing(self):
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_discord.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            # stav se mezitím změnil – Alice zmizela z DB
            storage.save_data("players.json", [])
            holder["confirm"] = _interaction(user=_admin_member(), guild=guild)
            await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        alice.add_roles.assert_not_awaited()
        embed, _ = _sent(holder["confirm"])
        self.assertIn("🔄 /sync discord – stav se změnil", embed.title)

    def test_confirm_partial_on_forbidden_never_crashes(self):
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC", forbid_add=True)
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_discord.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            holder["confirm"] = _interaction(user=_admin_member(), guild=guild)
            await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        embed, _ = _sent(holder["confirm"])
        self.assertIn("Úspěšně: **0 / 1**", embed.description)
        # audit se zapíše i při chybě (každá akce má ok/error)
        audits = storage.load_data(PLAYERSYNC_LOG_FILE, [])
        apply = [e for e in audits if e.get("mode") == "apply"]
        self.assertEqual(len(apply), 1)
        self.assertFalse(apply[0]["applied"][0]["ok"])

    def test_retired_tier_never_synced_no_view(self):
        storage.save_data(
            "players.json",
            [
                _player("OldMC", {"randompot": "RHT2"}, discord_id="1",
                        history={"randompot": [{"date": "01.01.2024", "tier": "HT3"}]}),
            ],
        )
        cog = Sync.__new__(Sync)
        inter = _interaction(
            user=_admin_member(), guild=_guild([_member(1, "OldMC")])
        )

        async def main():
            await Sync.sync_discord.callback(cog, inter, "apply")

        asyncio.run(main())
        embed, kwargs = _sent(inter)
        self.assertIsNone(kwargs.get("view"))
        self.assertIn("Žádné změny nelze aplikovat automaticky", embed.footer.text)

    def test_apply_idempotent_after_success(self):
        cog = Sync.__new__(Sync)
        alice = _member(1, "AliceMC")
        guild = _guild([alice])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_discord.callback(cog, inter, "apply")
            view = inter.followup.send.call_args.kwargs["view"]
            await view.confirm.callback(_interaction(user=_admin_member(), guild=guild))
            # role teď odpovídá DB → druhý běh nemá co dělat
            alice.roles.append(SimpleNamespace(id=101))
            holder["inter2"] = _interaction(user=_admin_member(), guild=guild)
            await Sync.sync_discord.callback(cog, holder["inter2"], "apply")

        asyncio.run(main())
        embed, kwargs = _sent(holder["inter2"])
        self.assertIsNone(kwargs.get("view"))
        self.assertIn("✅ /sync discord – vše v pořádku", embed.title)

    def test_preview_permission_denied(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.sync_discord.callback(cog, inter, "preview")

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )

    def test_confirm_view_permission_denied(self):
        view = SyncDiscordConfirmView(analysis={"fingerprint": "x"})
        inter = _interaction(user=_plain_member())

        async def main():
            await view.confirm.callback(inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()


# ---------------------------------------------------------------------------
# /sync web – DB → web/GitHub (services.websync)
# ---------------------------------------------------------------------------
class SyncWebTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

        # BobMC je v kanonické DB, ale ne na webu → k synchronizaci
        self.canonical = [
            _player("AliceMC", {"randompot": "HT3"},
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
            _player("BobMC", {"randompot": "HT2"},
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT2"}]}),
        ]
        self.web = [
            _player("AliceMC", {"randompot": "HT3"},
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
        ]
        storage.save_data("players.json", _deep(self.canonical))

    def _run(self, inter, mode, *, web=None, push=None):
        cog = Sync.__new__(Sync)
        fetch_result = (_deep(self.web), "sha", None) if web is None else web

        async def main():
            fetcher = mock.AsyncMock(return_value=fetch_result)
            pusher = mock.AsyncMock(return_value=push or (True, "✅ push", []))
            self.pusher = pusher
            with mock.patch.object(github_sync, "fetch_players", new=fetcher), \
                 mock.patch.object(github_sync, "push_players", new=pusher):
                await Sync.sync_web.callback(cog, inter, mode)

        asyncio.run(main())

    def test_preview_no_push(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, "preview")
        embed, kwargs = _sent(inter)
        self.assertIn("🔎 /sync web – náhled", embed.title)
        self.assertIsNone(kwargs.get("view"))
        self.pusher.assert_not_awaited()

    def test_apply_shows_view_without_pushing_yet(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, "apply")
        _, kwargs = _sent(inter)
        self.assertIsInstance(kwargs.get("view"), SyncWebConfirmView)
        self.pusher.assert_not_awaited()

    def test_confirm_pushes_and_reports_success(self):
        inter = _interaction(user=_admin_member())
        cog = Sync.__new__(Sync)
        holder = {}

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(_deep(self.web), "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players",
                new=mock.AsyncMock(return_value=(True, "✅ players.json na webu nahrazen", [])),
            ):
                await Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                holder["confirm"] = _interaction(user=_admin_member())
                await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        embed, _ = _sent(holder["confirm"])
        self.assertIn("✅ /sync web – web synchronizován", embed.title)
        logs = storage.load_data(WEBSYNC_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "apply" and e.get("status") == "success"
                            for e in logs))

    def test_confirm_push_failure_never_reported_as_success(self):
        inter = _interaction(user=_admin_member())
        cog = Sync.__new__(Sync)
        holder = {}

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(_deep(self.web), "sha", None)),
            ), mock.patch.object(
                # GITHUB_TOKEN zpráva = okamžitý break (bez retry spánku)
                github_sync, "push_players",
                new=mock.AsyncMock(
                    return_value=(False, "❌ GITHUB_TOKEN chybí pro zápis", [])
                ),
            ):
                await Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                holder["confirm"] = _interaction(user=_admin_member())
                await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        embed, _ = _sent(holder["confirm"])
        self.assertIn("❌ /sync web – synchronizace selhala", embed.title)
        logs = storage.load_data(WEBSYNC_LOG_FILE, [])
        failure = [e for e in logs if e.get("mode") == "apply"]
        self.assertTrue(failure)
        self.assertNotEqual(failure[-1].get("status"), "success")

    def test_no_token_cannot_read_nor_push(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, "apply", web=(None, None, None))
        embed, kwargs = _sent(inter)
        self.assertIn("⚠️ /sync web – nelze pokračovat", embed.title)
        self.assertIn("GITHUB_TOKEN", embed.description)
        self.assertIsNone(kwargs.get("view"))
        self.pusher.assert_not_awaited()

    def test_stale_nothing_pushes(self):
        inter = _interaction(user=_admin_member())
        cog = Sync.__new__(Sync)
        holder = {}

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(_deep(self.web), "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock()
            ) as pusher:
                await Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                # kanonická DB se mezitím změnila
                storage.save_data(
                    "players.json", _deep(self.canonical) + [
                        _player("CarolMC", {"randompot": "LT1"},
                                history={"randompot": [{"date": "x", "tier": "LT1"}]}),
                    ]
                )
                holder["confirm"] = _interaction(user=_admin_member())
                await view.confirm.callback(holder["confirm"])
                holder["pusher"] = pusher

        asyncio.run(main())
        holder["pusher"].assert_not_awaited()
        embed, _ = _sent(holder["confirm"])
        self.assertIn("🔄 /sync web – stav se změnil", embed.title)

    def test_empty_db_never_pushed(self):
        storage.save_data("players.json", [])
        inter = _interaction(user=_admin_member())
        self._run(inter, "apply")
        _, kwargs = _sent(inter)
        # žádný view, žádný push – prázdná DB se nikdy neposílá
        self.assertIsNone(kwargs.get("view"))
        self.pusher.assert_not_awaited()

    def test_permission_denied(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.sync_web.callback(cog, inter, "preview")

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )

    def test_confirm_view_permission_denied(self):
        view = SyncWebConfirmView(canonical_fingerprint="x")
        inter = _interaction(user=_plain_member())

        async def main():
            await view.confirm.callback(inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# /sync data – kontrola integrity + bezpečné opravy
# ---------------------------------------------------------------------------
class SyncDataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

        storage.save_data(
            "players.json",
            [
                _player("AliceMC", {"randompot": "HT3"},
                        history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
            ],
        )
        # Osamocený otevřený ticket bez kanálu → bezpečná oprava „zavřít".
        storage.save_data(
            "ht_tickets.json",
            {
                "1": {
                    "id": "1",
                    "status": "open",
                    "ownerId": "111",
                    "ign": "GhostMC",
                    "kit": "randompot",
                    "tier": "HT3",
                }
            },
        )

    def test_report_shows_repair_view(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member(), guild=_guild([]))

        async def main():
            await Sync.sync_data.callback(cog, inter)

        asyncio.run(main())
        embed, kwargs = _sent(inter)
        self.assertIn("🔍 /sync data – kontrola integrity", embed.title)
        self.assertIsInstance(kwargs.get("view"), SyncDataRepairView)
        logs = storage.load_data(DATACHECK_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "check" for e in logs))

    def test_repair_closes_ticket_keeps_record(self):
        cog = Sync.__new__(Sync)
        guild = _guild([])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_data.callback(cog, inter)
            view = inter.followup.send.call_args.kwargs["view"]
            holder["repair"] = _interaction(user=_admin_member(), guild=guild)
            await view.repair.callback(holder["repair"])

        asyncio.run(main())
        tickets = storage.load_data("ht_tickets.json", {})
        self.assertEqual(tickets["1"]["status"], "closed")  # záznam zůstává
        self.assertEqual(len(tickets), 1)
        embed, _ = _sent(holder["repair"])
        self.assertIn("bezpečné opravy", embed.title)
        logs = storage.load_data(DATACHECK_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "repair" for e in logs))

    def test_corrupt_data_aborts_nothing_changes(self):
        _write_corrupt("players.json")
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())
        run_datacheck = mock.AsyncMock()

        async def main():
            with mock.patch("cogs.sync.run_datacheck", new=run_datacheck):
                await Sync.sync_data.callback(cog, inter)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("❌ /sync data – poškozená data", embed.title)
        run_datacheck.assert_not_awaited()

    def test_repair_view_corruption_never_performs(self):
        cog = Sync.__new__(Sync)
        guild = _guild([])
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {}

        async def main():
            await Sync.sync_data.callback(cog, inter)
            view = inter.followup.send.call_args.kwargs["view"]
            holder["repair"] = _interaction(user=_admin_member(), guild=guild)
            with mock.patch(
                "cogs.sync.run_datacheck",
                new=mock.AsyncMock(
                    side_effect=DataCorruptionError("players.json corrupt")
                ),
            ):
                await view.repair.callback(holder["repair"])

        asyncio.run(main())
        embed, _ = _sent(holder["repair"])
        self.assertIn("❌ /sync data – poškozená data", embed.title)
        tickets = storage.load_data("ht_tickets.json", {})
        self.assertEqual(tickets["1"]["status"], "open")  # nic se nezměnilo

    def test_report_permission_denied(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.sync_data.callback(cog, inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )

    def test_repair_view_permission_denied(self):
        view = SyncDataRepairView()
        inter = _interaction(user=_plain_member())

        async def main():
            await view.repair.callback(inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Deprecated aliasy – FUNKČNÍ, delegují na sdílené orchestrace + upozorní
# ---------------------------------------------------------------------------
class DeprecatedAliasTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)
        storage.save_data("players.json", [])
        storage.save_data("kit_roles.json", {})

    def test_playersync_alias_end_to_end_announces_deprecated(self):
        storage.save_data(
            "players.json",
            [
                _player("AliceMC", {"randompot": "HT3"}, discord_id="1",
                        history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
            ],
        )
        cog = Sync.__new__(Sync)
        inter = _interaction(
            user=_admin_member(), guild=_guild([_member(1, "AliceMC")])
        )

        async def main():
            await Sync.playersync_preview.callback(cog, inter)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn(
            "⚠️ Deprecated – použij /sync discord mode:preview", embed.footer.text
        )

    def test_playersync_apply_delegates_to_run_discord_apply(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            with mock.patch.object(Sync, "_run_discord", new=mock.AsyncMock()) as run:
                await Sync.playersync_apply.callback(cog, inter)
                run.assert_awaited_once_with(
                    inter, mode="apply",
                    note="⚠️ Deprecated – použij /sync discord mode:apply.",
                )

        asyncio.run(main())

    def test_websync_preview_delegates_to_run_web(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            with mock.patch.object(Sync, "_run_web", new=mock.AsyncMock()) as run:
                await Sync.websync_preview.callback(cog, inter)
                run.assert_awaited_once_with(
                    inter, mode="preview",
                    note="⚠️ Deprecated – použij /sync web mode:preview.",
                )

        asyncio.run(main())

    def test_websync_apply_delegates_to_run_web(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            with mock.patch.object(Sync, "_run_web", new=mock.AsyncMock()) as run:
                await Sync.websync_apply.callback(cog, inter)
                run.assert_awaited_once_with(
                    inter, mode="apply",
                    note="⚠️ Deprecated – použij /sync web mode:apply.",
                )

        asyncio.run(main())

    def test_checkweb_preview_delegates_to_run_check(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            with mock.patch.object(Sync, "_run_check", new=mock.AsyncMock()) as run:
                await Sync.checkweb_preview.callback(cog, inter)
                run.assert_awaited_once_with(
                    inter, area="all",
                    note="⚠️ Deprecated – použij /sync check.",
                )

        asyncio.run(main())

    def test_checkweb_apply_delegates_to_run_checkweb_apply(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            with mock.patch.object(
                Sync, "_run_checkweb_apply", new=mock.AsyncMock()
            ) as run:
                await Sync.checkweb_apply.callback(cog, inter)
                run.assert_awaited_once()
                self.assertIn("Deprecated", run.await_args.kwargs["note"])

        asyncio.run(main())

    def test_datacheck_delegates_to_run_data(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            with mock.patch.object(Sync, "_run_data", new=mock.AsyncMock()) as run:
                await Sync.datacheck.callback(cog, inter)
                run.assert_awaited_once_with(
                    inter, note="⚠️ Deprecated – použij /sync data."
                )

        asyncio.run(main())

    def test_datacheck_alias_end_to_end_runs_and_announces(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_admin_member())

        async def main():
            await Sync.datacheck.callback(cog, inter)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("✅ /sync data – vše v pořádku", embed.title)
        self.assertIn("⚠️ Deprecated – použij /sync data", embed.footer.text)

    def test_alias_permission_denied(self):
        cog = Sync.__new__(Sync)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.datacheck.callback(cog, inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )


class SyncEmbedPackTests(unittest.TestCase):
    """Embed pack (/sync check • data • discord • web • checkweb).

    Regresní testy pro produkční bug „In embeds.0.fields.0.value: Must be
    1024 or fewer in length": diagnostika se NIKDY nezkracuje ani nedělá
    útržky „… a dalších N" – dlouhé seznamy se rozdělí na víc polí / embedů
    (na hranicích řádků), vše zůstává a Discord limity se nikdy nepřekročí.
    """

    @staticmethod
    def _field_lines(embeds):
        """Všechny řádky všech polí všech embedů v pořadí."""
        out = []
        for embed in embeds:
            for field in embed.fields:
                out.extend(field.value.split("\n"))
        return out

    @staticmethod
    def _reconstruct(embeds):
        """Rekonstrukce původních řádků z packu (odstraní „» " pokračování).

        Každý řádek začínající „» " je pokračováním předchozího řádku –
        připojí se bez prefixu, takže porovnání s originálem ověří, že se
        ŽÁDNÝ znak diagnostiky neztratil.
        """
        result = []
        for line in SyncEmbedPackTests._field_lines(embeds):
            if line.startswith("» ") and result:
                result[-1] += line[len("» "):]
            else:
                result.append(line)
        return result

    @staticmethod
    def _embed_total(embed):
        """Počet znaků embedu (title + description + pole + footer)."""
        return (
            len(embed.title or "")
            + len(embed.description or "")
            + sum(len(f.name) + len(f.value) + 2 for f in embed.fields)
            + len(embed.footer.text if embed.footer else "")
        )

    def test_field_value_exactly_1024_stays_single_field(self):
        line = "x" * 1024  # přesně na limitu – nesmí se rozdělit ani zkrátit
        embeds = build_embed_pack(
            title="t", description="d", color=0, sections=[("Pole", [line])]
        )
        self.assertEqual(len(embeds), 1)
        self.assertEqual(len(embeds[0].fields), 1)
        self.assertEqual(embeds[0].fields[0].value, line)

    def test_field_value_over_1024_split_without_loss(self):
        line = "slovo " * 400  # 2400 znaků > 1024
        embeds = build_embed_pack(
            title="t", description="d", color=0, sections=[("Pole", [line])]
        )
        for embed in embeds:
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
        self.assertEqual(self._reconstruct(embeds), [line])

    def test_field_value_many_long_lines_no_truncation_marker(self):
        lines = [f"nález {i}: " + "d" * 120 for i in range(60)]
        embeds = build_embed_pack(
            title="t", description="d", color=0, sections=[("Pole", lines)]
        )
        blob = "\n".join(self._field_lines(embeds))
        self.assertNotIn("zkráceno", blob)
        self.assertNotIn("a dalších", blob)
        self.assertEqual(self._reconstruct(embeds), lines)

    def test_many_long_lines_span_multiple_embeds_within_limits(self):
        lines = [f"nález {i}: " + "x" * 200 for i in range(200)]
        embeds = build_embed_pack(
            title="t",
            description="d",
            color=0,
            footer="audit",
            sections=[("Pole", lines)],
        )
        self.assertGreater(len(embeds), 1)
        for embed in embeds:
            self.assertLessEqual(len(embed.fields), 25)
            self.assertLessEqual(len(embed.title or ""), 256)
            self.assertLessEqual(len(embed.description or ""), 4096)
            for field in embed.fields:
                self.assertLessEqual(len(field.name), 256)
                self.assertLessEqual(len(field.value), 1024)
            self.assertLessEqual(self._embed_total(embed), 6000)
            self.assertTrue(embed.fields)  # žádný prázdný embed
        self.assertEqual(self._reconstruct(embeds), lines)

    def test_field_count_limited_to_25_per_embed(self):
        sections = [(f"sekce {i}", [f"řádek {i}"]) for i in range(30)]
        embeds = build_embed_pack(
            title="t", description="d", color=0, sections=sections
        )
        self.assertGreater(len(embeds), 1)
        for embed in embeds:
            self.assertLessEqual(len(embed.fields), 25)
        total_fields = sum(len(e.fields) for e in embeds)
        self.assertEqual(total_fields, 30)  # žádná sekce se neztratila

    def test_empty_sections_render_placeholder_single_embed(self):
        embeds = build_embed_pack(
            title="✅ /sync check – vše v pořádku",
            description="Nalezeno **0** problémů.",
            color=0x10B981,
            sections=[("Nálezů celkem: 0", [])],
        )
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].fields[0].value, "_žádné_")

    def test_datacheck_embed_many_findings_preserved(self):
        # Přesně scénář produkčního bugu: /sync data s hromadou nálezů.
        findings = [
            {"kind": "orphan_ticket", "message": f"ticket {i}: " + "t" * 100}
            for i in range(200)
        ]
        report = {
            "has_issues": True,
            "total_findings": len(findings),
            "summary": {"orphan_ticket": len(findings)},
            "findings": findings,
            "repairable": {"close_ticket": [], "normalize_tier": []},
            "repairable_count": 0,
        }
        embeds = _datacheck_embed(report)
        self.assertGreater(len(embeds), 1)
        for embed in embeds:
            self.assertLessEqual(len(embed.fields), 25)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
            self.assertLessEqual(self._embed_total(embed), 6000)
        self.assertEqual(self._reconstruct(embeds), [f["message"] for f in findings])

    def test_datacheck_embed_no_issues_single_embed(self):
        embeds = _datacheck_embed(
            {
                "has_issues": False,
                "summary": {},
                "findings": [],
                "repairable": {"close_ticket": [], "normalize_tier": []},
                "repairable_count": 0,
            }
        )
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].fields, [])

    def test_check_embed_many_long_findings_no_silent_loss(self):
        items = [
            {"severity": "warning", "kind": "k", "message": f"nález {i} " + "x" * 300}
            for i in range(40)
        ]
        embeds = _check_embed(
            items,
            counts={"error": 0, "conflict": 0, "warning": len(items)},
            website_source="GitHub",
            area="all",
            corrupt=[],
            repairable_count=0,
        )
        self.assertGreater(len(embeds), 1)
        for embed in embeds:
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
            self.assertLessEqual(self._embed_total(embed), 6000)
        self.assertEqual(self._reconstruct(embeds), [i["message"] for i in items])

    def test_send_embed_pack_splits_messages_of_ten_keeps_ephemeral_and_view(self):
        followup = mock.MagicMock()
        followup.send = mock.AsyncMock()
        view = object()
        embeds = [discord.Embed(title=f"e{i}") for i in range(23)]

        async def main():
            await _send_embed_pack(followup, embeds, view=view)

        asyncio.run(main())
        self.assertEqual(followup.send.await_count, 3)
        for call in followup.send.await_args_list:
            self.assertLessEqual(len(call.kwargs["embeds"]), 10)
            self.assertEqual(call.kwargs["ephemeral"], True)
        first = followup.send.await_args_list[0].kwargs
        self.assertEqual(len(first["embeds"]), 10)
        self.assertIs(first["view"], view)
        # view jen u první zprávy, pokračování čistá
        for call in followup.send.await_args_list[1:]:
            self.assertNotIn("view", call.kwargs)


if __name__ == "__main__":
    unittest.main()
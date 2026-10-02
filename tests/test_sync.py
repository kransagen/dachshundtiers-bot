"""Cog-level testy centrální synchronizace – cogs/sync.py.

Pokrývají /sync (orchestrace, logika zůstává ve službách):

- ``/sync check``    – read-only diagnostika: severity OK/WARNING/CONFLICT/
                       ERROR, filter podle oblasti (area), web nedostupný se
                       NEPOSÍLÁ do analyze_websync (žádné falešné nálezy),
                       idempotence, audit v checkweb_log,
- ``/sync discord``  – jediná brána Discord → PostgreSQL: dry run náhled,
                       zápis mirroru až po potvrzení tlačítkem (jednou);
                       ROLE SE NIKDY NEMĚNÍ (add_roles / remove_roles / edit
                       = 0), bez PostgreSQL → tvrdá chyba, DB selhání → loud
                       error, strukturální kontrakt: cog neobsahuje žádný
                       mutující helper,
- ``/sync web``      – preview nic neposílá; apply → view; potvrzení nahraje
                       přes services.websync (GitHub), failure NIKDY není
                       success, bez GITHUB_TOKEN → nelze, prázdná DB se
                       neposílá, stale → nic,
- povrch příkazů je jen discord / web / check / rollback,
- administrátorský gate (has_admin_role / ADMIN_ROLE_IDS) u KAŽDÉ operace
  i u potvrzovacích tlačítek.
"""

import asyncio
import inspect
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
import github_sync
import storage
import cogs.sync as sync_mod
from services import permissions
from services.checkweb import CHECKWEB_LOG_FILE
from services.websync import WEBSYNC_LOG_FILE

from db.repositories.sync_audit import SYNC_RUN_SUCCESS
from db.services import DiscordSyncService
from db.services.mirror_sync import CHANGE_TIER_CHANGED, SyncChange

from cogs.sync import (
    Sync,
    SyncDiscordConfirmView,
    SyncWebConfirmView,
    _check_embed,
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
    # DB režim se v single-mode testech aktivuje jen explicitním nastavením;
    # MagicMock by auto-generoval pravdivý db_session_factory → DB cesta.
    inter.client.db_session_factory = None
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
        #   warning   = MISSING_DISCORD_ROLE + websync missing_player
        #   error     = Carol je v DB 2× (DUPLICATE_PLAYER)
        self.players = [
            _player("AliceMC", {"randompot": "HT2"}, discord_id="1",
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT2"}]}),
            _player("BobMC", {"randompot": "HT3"}, discord_id="2",
                    history={"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}),
            _player("CarolMC", {"randompot": "RHT2"}, discord_id="3",
                    history={"randompot": [{"date": "01.01.2024", "tier": "HT3"}]}),
            _player("DaveMC", {"randompot": "NONSENSE"}, discord_id="4"),
            _player("carolmc", {"randompot": "HT2"}, discord_id="5"),
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
        # DB zdroje: /sync check čte kanonickou podobu hráčů a role mapu
        # výhradně z PostgreSQL (_canonical_players/_kit_role_maps_or_empty/
        # kit_display_map). Kanonická data v testu = same obsah jako players.json
        # export (player_current_tiers mirror), který čte legacy datacheck
        # tooling; ostatní analýzy jsou čisté funkce.
        patch = mock.patch.object(
            sync_mod,
            "_canonical_players",
            new=mock.AsyncMock(return_value=_deep(self.players)),
        )
        patch.start()
        self.addCleanup(patch.stop)

        patch = mock.patch.object(
            sync_mod,
            "_kit_role_maps_or_empty",
            new=mock.AsyncMock(
                return_value={"randompot": {"HT3": 101, "HT2": 102}}
            ),
        )
        patch.start()
        self.addCleanup(patch.stop)

        patch = mock.patch.object(
            sync_mod,
            "kit_display_map",
            new=mock.AsyncMock(return_value={"randompot": "RandomPot"}),
        )
        patch.start()
        self.addCleanup(patch.stop)

        # G0 health report je samostatný embed z db.services.health – v tomhle
        # scénáři nepřidáváme, počty severit zůstávají z 3-cestné analýzy.
        patch = mock.patch.object(
            sync_mod, "_db_health_embed", new=mock.AsyncMock(return_value=[])
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, inter, area="all", *, web=(True,)):
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())  # DB režim

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
        self.assertIn("⚠️ WARNING: **5**", embed.description)
        self.assertIn("Nálezů celkem: **7**", embed.description)
        self.assertIn("**GitHub**", embed.description)
        field_names = [f.name for f in embed.fields]
        self.assertTrue(any("ERROR (1)" in n for n in field_names))
        self.assertTrue(any("CONFLICT (1)" in n for n in field_names))
        self.assertTrue(any("WARNING (5)" in n for n in field_names))
        # read-only – data beze změny
        self.assertEqual(storage.load_data("players.json", []), self.players)

    def test_check_writes_into_existing_audit_logs(self):
        inter = _interaction(user=_admin_member())
        self._run(inter)
        cw_log = storage.load_data(CHECKWEB_LOG_FILE, [])
        self.assertTrue(any(e.get("mode") == "preview" for e in cw_log))

    def test_check_area_filter_web(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, area="web")
        embed, _ = _sent(inter)
        self.assertIn("⚠️ WARNING: **3**", embed.description)  # websync
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
        self.assertIn("⚠️ WARNING: **2**", embed.description)

    def test_check_website_unavailable_skips_websync_no_false_positives(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, web=False)
        embed, _ = _sent(inter)
        self.assertIn("nedostupný", embed.description)
        # warning: 2 checkweb MISSING – ŽÁDNÝ websync missing_player (falešné nálezy)
        self.assertIn("⚠️ WARNING: **2**", embed.description)
        self.assertIn("❌ ERROR: **1**", embed.description)

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
# /sync discord – observe-only: Discord → PostgreSQL mirror (DiscordSyncService)
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
        storage.save_data("players.json", [])
        storage.save_data("kit_roles.json", {})

    def _make_outcome(self, **overrides):
        base = dict(
            sync_run_id=7,
            status=SYNC_RUN_SUCCESS,
            scanned_members=1,
            observations_applied=1,
            anomalies=0,
            unknown_members=0,
            failed_members=0,
            unknown_roles=(),
            tier_changes=0,
            changes=(),
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_observe_never_mutates_discord_roles(self):
        # Kontrakt: add_roles / remove_roles / edit se u /sync discord NESMÍ
        # nikdy zavolat – Discord zůstává jedinou autoritou aktuálních tierů.
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        alice = _member(1, "AliceMC")
        alice.edit = mock.AsyncMock()
        inter = _interaction(user=_admin_member(), guild=_guild([alice]))

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.return_value = self._make_outcome()
                await Sync.sync_discord.callback(cog, inter)

        asyncio.run(main())
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()

    def test_preview_runs_dry_run_and_offers_no_button_when_in_sync(self):
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        alice = _member(1, "AliceMC")
        inter = _interaction(user=_admin_member(), guild=_guild([alice]))

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.return_value = self._make_outcome(sync_run_id=None)
                await Sync.sync_discord.callback(cog, inter)
                return sync_guild.await_args.kwargs

        kwargs = asyncio.run(main())
        self.assertTrue(kwargs["dry_run"])
        embed, sent = _sent(inter)
        self.assertIn("Discord → PostgreSQL mirror (náhled)", embed.title)
        self.assertIn("Observe-only", embed.footer.text)
        self.assertNotIn("view", sent)

    def test_preview_with_changes_shows_confirm_button(self):
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        inter = _interaction(user=_admin_member(), guild=_guild([_member(1, "A")]))
        changes = (
            SyncChange(
                member_id=1, kind=CHANGE_TIER_CHANGED, kit_key="ht3",
                old_tier="HT4", new_tier="HT3",
            ),
            SyncChange(member_id=2, kind="unknown_player"),
        )

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.return_value = self._make_outcome(
                    sync_run_id=None, tier_changes=1, anomalies=1, changes=changes
                )
                await Sync.sync_discord.callback(cog, inter)

        asyncio.run(main())
        embed, sent = _sent(inter)
        self.assertIsInstance(sent["view"], SyncDiscordConfirmView)
        text = " ".join(f.value for f in embed.fields)
        self.assertIn("HT4 → **HT3**", text)
        self.assertIn("unknown_player", text)

    def test_confirm_writes_mirror_once_and_never_touches_roles(self):
        alice = _member(1, "AliceMC")
        alice.edit = mock.AsyncMock()
        guild = _guild([alice])
        view = SyncDiscordConfirmView()
        inter = _interaction(user=_admin_member(), guild=guild)
        inter.client.db_session_factory = object()
        again = _interaction(user=_admin_member(), guild=guild)

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.return_value = self._make_outcome()
                await view.confirm.callback(inter)
                await view.confirm.callback(again)
                return sync_guild

        sync_guild = asyncio.run(main())
        self.assertEqual(sync_guild.await_count, 1)
        self.assertFalse(sync_guild.await_args.kwargs["dry_run"])
        embed, _ = _sent(inter)
        self.assertIn("Run: #7", embed.footer.text)
        again.response.send_message.assert_awaited_once_with(
            "✅ Synchronizace už proběhla.", ephemeral=True
        )
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()

    def test_confirm_db_failure_is_loud_and_allows_retry(self):
        view = SyncDiscordConfirmView()
        inter = _interaction(user=_admin_member(), guild=_guild([]))
        inter.client.db_session_factory = object()

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.side_effect = OSError("connection refused")
                await view.confirm.callback(inter)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("databáze selhala", embed.title)
        self.assertFalse(view.finished)

    def test_confirm_permission_denied(self):
        view = SyncDiscordConfirmView()
        inter = _interaction(user=_plain_member())

        async def main():
            await view.confirm.callback(inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )

    def test_no_db_configured_is_loud_and_never_calls_service(self):
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=None)
        inter = _interaction(user=_admin_member(), guild=_guild([]))

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                await Sync.sync_discord.callback(cog, inter)
                self.assertEqual(sync_guild.await_count, 0)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("PostgreSQL není nakonfigurováno", embed.title)
        self.assertIn("nic se neměnilo", embed.description)

    def test_db_failure_is_loud_and_discord_untouched(self):
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        alice = _member(1, "AliceMC")
        alice.edit = mock.AsyncMock()
        inter = _interaction(user=_admin_member(), guild=_guild([alice]))

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.side_effect = OSError("connection refused")
                await Sync.sync_discord.callback(cog, inter)

        asyncio.run(main())
        embed, _ = _sent(inter)
        self.assertIn("databáze selhala", embed.title)
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()

    def test_permission_denied(self):
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=None)
        inter = _interaction(user=_plain_member())

        async def main():
            await Sync.sync_discord.callback(cog, inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Strukturální kontrakt: cogs/sync.py nesmí přímo mutovat Discord role
# ---------------------------------------------------------------------------
class SyncStructuralContractTests(unittest.TestCase):
    def test_command_surface_is_discord_web_check_rollback(self):
        self.assertEqual(
            {c.name for c in Sync.sync.commands},
            {"discord", "web", "check", "rollback"},
        )
        for removed in ("playersync", "websync", "checkweb", "datacheck"):
            self.assertFalse(hasattr(Sync, removed))

    def test_no_role_mutation_helpers_or_calls_in_cog(self):
        src = inspect.getsource(Sync)
        for forbidden in (
            "apply_role_actions()",
            "auto_grant_kit_role",
            ".add_roles(",
            ".remove_roles(",
            "member.edit(",
        ):
            self.assertNotIn(forbidden, src)


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

    def _run(self, inter, mode, *, web=None, push=None, canonical=None):
        # Kanonická podoba hráčů jde z PostgreSQL (_canonical_for_export →
        # export_players) – nikdy ne z players.json.
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        fetch_result = (_deep(self.web), "sha", None) if web is None else web
        canonical_list = _deep(self.canonical) if canonical is None else canonical

        async def main():
            fetcher = mock.AsyncMock(return_value=fetch_result)
            pusher = mock.AsyncMock(return_value=push or (True, "✅ push", []))
            self.pusher = pusher
            with mock.patch.object(github_sync, "fetch_players", new=fetcher), \
                 mock.patch.object(github_sync, "push_players", new=pusher), \
                 mock.patch.object(
                     sync_mod,
                     "_canonical_for_export",
                     new=mock.AsyncMock(return_value=canonical_list),
                 ):
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
        cog.bot = SimpleNamespace(db_session_factory=object())
        holder = {}

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(_deep(self.web), "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players",
                new=mock.AsyncMock(return_value=(True, "✅ players.json na webu nahrazen", [])),
            ), mock.patch.object(
                sync_mod, "_canonical_for_export",
                new=mock.AsyncMock(return_value=_deep(self.canonical)),
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
        cog.bot = SimpleNamespace(db_session_factory=object())
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
            ), mock.patch.object(
                sync_mod, "_canonical_for_export",
                new=mock.AsyncMock(return_value=_deep(self.canonical)),
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

    def test_pg_export_failure_sends_nothing(self):
        """H6 dokončeno: selhání exportu z PostgreSQL NIKDY nespadá na legacy
        players.json – /sync web neproběhne a na web se nic nepošle."""
        inter = _interaction(user=_admin_member())
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        pusher = mock.AsyncMock()

        async def main():
            with mock.patch.object(
                sync_mod,
                "_canonical_for_export",
                new=mock.AsyncMock(side_effect=RuntimeError("DB down")),
            ), mock.patch.object(
                github_sync, "push_players", new=pusher,
            ):
                await Sync.sync_web.callback(cog, inter, "apply")

        asyncio.run(main())
        msg = inter.followup.send.await_args.args[0]
        self.assertIn("PostgreSQL nedostupný", msg)
        self.assertIn("nic jiného než data z DB", msg)
        pusher.assert_not_awaited()

    def test_stale_nothing_pushes(self):
        inter = _interaction(user=_admin_member())
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        holder = {}
        calls = {"n": 0}

        def canonical_bump(*_args, **_kwargs):
            # 1. volání = náhled (apply), 2. volání = potvrzení – DB se změnila
            calls["n"] += 1
            if calls["n"] == 1:
                return _deep(self.canonical)
            return _deep(self.canonical) + [
                _player("CarolMC", {"randompot": "LT1"},
                        history={"randompot": [{"date": "x", "tier": "LT1"}]}),
            ]

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(_deep(self.web), "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock()
            ) as pusher, mock.patch.object(
                sync_mod, "_canonical_for_export",
                new=mock.AsyncMock(side_effect=canonical_bump),
            ):
                await Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                holder["confirm"] = _interaction(user=_admin_member())
                await view.confirm.callback(holder["confirm"])
                holder["pusher"] = pusher

        asyncio.run(main())
        holder["pusher"].assert_not_awaited()
        embed, _ = _sent(holder["confirm"])
        self.assertIn("🔄 /sync web – stav se změnil", embed.title)

    def test_empty_db_never_pushed(self):
        inter = _interaction(user=_admin_member())
        self._run(inter, "apply", canonical=[])
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
        )
        self.assertGreater(len(embeds), 1)
        for embed in embeds:
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
            self.assertLessEqual(self._embed_total(embed), 6000)
        self.assertEqual(self._reconstruct(embeds), [i["message"] for i in items])

    def test_split_long_line_with_space_after_prefix_terminates(self):
        import signal

        from cogs.sync import _split_long_line

        line = "a" * 10 + " " + "b" * 3000
        signal.alarm(5)
        try:
            chunks = _split_long_line(line, 1024, "» ")
        finally:
            signal.alarm(0)
        self.assertTrue(all(len(c) <= 1024 for c in chunks))
        joined = chunks[0] + "".join(c[len("» "):] for c in chunks[1:])
        self.assertEqual(joined, line)

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
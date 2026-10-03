"""Phase E, E7 + Phase B (B1/B2) — GitHub export is strictly downstream-only.

Postgres/DB is NOT a source for the export and the export NEVER changes
Discord:

- /sync web export reads the canonical roster EXCLUSIVELY from PostgreSQL
  (``_canonical_for_export`` → ``export_players``; players.json je export
  artifact, nikdy zdroj); bez PostgreSQL (session_factory=None / nefunkční)
  export NEproběhne – žádný legacy JSON fallback;
- a failed export push is reported LOUDLY and Discord roles are never
  touched (no add_roles / remove_roles / edit anywhere in the export path);
- the confirmed canonical fingerprint is re-verified before pushing — a
  stale confirmation pushes NOTHING (never automatically anything extra);
- the export path performs zero Discord role mutation calls by construction.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import github_sync
import storage
from services import permissions
from services.websync import WEBSYNC_LOG_FILE


def _admin_member(rid: int = 999):
    return SimpleNamespace(
        id=999,
        roles=[SimpleNamespace(id=rid, name="Vedení")],
        guild_permissions=SimpleNamespace(administrator=False),
    )


def _interaction(user=None, guild=None):
    inter = mock.MagicMock()
    inter.user = user if user is not None else _admin_member()
    inter.guild = guild if guild is not None else mock.MagicMock()
    inter.response = mock.MagicMock()
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup = mock.MagicMock()
    inter.followup.send = mock.AsyncMock()
    inter.message = SimpleNamespace()
    inter.edit_original_response = mock.AsyncMock()
    return inter


def _player(discord_id, mode="HT3"):
    return {
        "username": f"Player{discord_id}",
        "discordId": discord_id,
        "modes": {"randompot": mode},
        "history": {"randompot": [{"date": "01.01.2026", "tier": mode}]},
    }


def _web_only():
    return [_player(1)]


def _canonical():
    return [_player(1), _player(2)]


def _guild(members=()):
    by_id = {int(m.id): m for m in members}
    g = mock.MagicMock()
    g.members = list(members)
    g.get_member.side_effect = lambda mid: by_id.get(mid)
    return g


class GitHubExportDownstreamOnlyTests(unittest.TestCase):
    """Export je downstream-only: DB down ho nezablokuje, neúspěch exportu
    nikdy nemění Discord a potvrzení se ověřuje proti otisku kanoniky."""

    CANONICAL = _canonical()
    WEB = _web_only()

    def setUp(self):
        from cogs.sync import Sync

        self.Sync = Sync
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)
        self._clear_websync_log()

    def _clear_websync_log(self):
        if storage.data_exists(WEBSYNC_LOG_FILE):
            storage.delete(WEBSYNC_LOG_FILE)

    def _run_apply(
        self,
        *,
        push_result=(True, "✅ players.json na webu nahrazen", []),
        fetch_returns=None,
        canonical=None,
        guild=None,
    ):
        """Spustí /sync web apply + potvrzení; vrací dict s výsledky.

        Kanonická podoba hráčů přichází z PostgreSQL exportu
        (``_canonical_for_export``) – mockuje se na „DB'' zdroj, nikdy to
        není players.json. Push/fetch mocky a Discord interakce se čtou
        UVNITŘ ``main`` – ``mock.patch.object`` se po ``asyncio.run`` uklidí.
        """
        from cogs.sync import SyncWebConfirmView

        cog = self.Sync.__new__(self.Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        inter = _interaction(
            user=_admin_member(), guild=guild if guild is not None else mock.MagicMock()
        )
        holder = {"pusher": None, "confirm": None}
        canonical_list = [dict(p) for p in self.CANONICAL] if canonical is None else canonical

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=fetch_returns or
                                   (self.WEB, "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players",
                new=mock.AsyncMock(return_value=push_result),
            ) as pusher, mock.patch(
                "cogs.sync._canonical_for_export",
                new=mock.AsyncMock(return_value=canonical_list),
            ):
                holder["pusher"] = pusher
                await self.Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                self.assertIsInstance(view, SyncWebConfirmView)
                holder["confirm"] = _interaction(
                    user=_admin_member(),
                    guild=guild if guild is not None else mock.MagicMock(),
                )
                await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        return holder

    def _sent(self, inter):
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

    def test_export_runs_from_postgres_and_succeeds(self):
        """Export čte kanoniku EXKLUZIVNĚ z PostgreSQL exportu (_canonical_for_export
        → export_players; players.json se nečte): proběhne a nahraje na web."""
        res = self._run_apply()
        embed, _ = self._sent(res["confirm"])
        self.assertIn("✅ /sync web – web synchronizován", embed.title)
        logs = storage.load_data(WEBSYNC_LOG_FILE, [])
        apply_events = [e for e in logs if e.get("mode") == "apply"]
        self.assertTrue(apply_events)
        self.assertEqual(apply_events[-1].get("status"), "success")
        pusher = res["pusher"]
        self.assertTrue(pusher.await_args)
        # Druhý argument push_players je merge/build_fn: aplikuje čistou
        # kanoniku BEZ ohledu na aktuální web → obsahuje oba hráče.
        build_fn = pusher.await_args.args[1]
        merged = build_fn([])
        self.assertEqual(
            [p["username"] for p in merged],
            [p["username"] for p in self.CANONICAL],
        )

    def test_export_failure_is_loud_and_discord_untouched(self):
        """Neúspěšný push (GitHub selže): loud „selhala“ zpráva, žádná role
        mutace, žádný success záznam v logu."""
        alice = SimpleNamespace(
            id=1, add_roles=mock.AsyncMock(), remove_roles=mock.AsyncMock(),
            edit=mock.AsyncMock(),
        )
        guild = _guild([alice])
        res = self._run_apply(
            push_result=(False, "❌ Web se nepodařilo přečíst: 500", ["500"]),
            guild=guild,
        )
        embed, _ = self._sent(res["confirm"])
        self.assertIn("❌ /sync web – synchronizace selhala", embed.title)
        logs = storage.load_data(WEBSYNC_LOG_FILE, [])
        failures = [e for e in logs if e.get("mode") == "apply"]
        self.assertTrue(failures)
        self.assertNotEqual(failures[-1].get("status"), "success")
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()

    def test_export_failure_no_token_never_pushes(self):
        """GITHUB_TOKEN chybí: export se hlasitě zalomí, na web se nic
        nenahraje a Discord zůstává nedotčený."""
        alice = SimpleNamespace(
            id=1, add_roles=mock.AsyncMock(), remove_roles=mock.AsyncMock(),
            edit=mock.AsyncMock(),
        )
        guild = _guild([alice])
        res = self._run_apply(
            push_result=(False, "❌ GITHUB_TOKEN chybí pro zápis", []),
            guild=guild,
        )
        embed, _ = self._sent(res["confirm"])
        self.assertIn("❌ /sync web – synchronizace selhala", embed.title)
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()

    def test_export_path_never_mutates_discord_by_construction(self):
        """Statická záruka: exportní cesta (preview_website → sync_website →
        SyncWebConfirmView) neobsahuje žádné Discord role mutace."""
        from cogs import sync
        from services import websync

        mutation_calls = (
            ".add_roles(",
            ".remove_roles(",
            ".edit_roles(",
            "member.edit(",
            "grant_kit_role(",
            "auto_grant_kit_role(",
            "commit_promotion_with_wedge(",
        )
        for module in (sync, websync, github_sync):
            src = __import__(module.__name__, fromlist=["*"]).__file__
            with open(src, encoding="utf-8") as fh:
                body = fh.read()
            for call in mutation_calls:
                self.assertNotIn(call, body)

    def test_confirmation_fingerprint_stale_pushes_nothing(self):
        """PostgreSQL export se mezi náhledem a potvrzením změnil → nic se
        na web neposílá (export nikdy nepřidá nic automaticky navíc) a
        Discord se nedotýká."""
        alice = SimpleNamespace(
            id=1, add_roles=mock.AsyncMock(), remove_roles=mock.AsyncMock(),
            edit=mock.AsyncMock(),
        )
        guild = _guild([alice])
        cog = self.Sync.__new__(self.Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        inter = _interaction(user=_admin_member(), guild=guild)
        holder = {"pusher": None, "confirm": None}
        calls = {"n": 0}

        def canonical_bump(*_args, **_kwargs):
            # 1. volání = náhled (apply), 2. volání = potvrzení – export
            # z PostgreSQL se mezitím změnil (nový hráč).
            calls["n"] += 1
            if calls["n"] == 1:
                return [dict(p) for p in self.CANONICAL]
            return [dict(p) for p in self.CANONICAL] + [_player(3)]

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(self.WEB, "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players", new=mock.AsyncMock(),
            ) as pusher, mock.patch(
                "cogs.sync._canonical_for_export",
                new=mock.AsyncMock(side_effect=canonical_bump),
            ):
                holder["pusher"] = pusher
                await self.Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                holder["confirm"] = _interaction(user=_admin_member(), guild=guild)
                await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        embed, _ = self._sent(holder["confirm"])
        self.assertIn("🔄 /sync web – stav se změnil", embed.title)
        holder["pusher"].assert_not_awaited()
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()
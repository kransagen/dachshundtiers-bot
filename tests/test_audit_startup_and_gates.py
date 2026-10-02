"""Regresní testy oprav z auditu (startup, reconnect, permission brány,
konfigurační fallbacky, purge fronty).

Každá testovací funkce odpovídá jednomu nálezu z bezpečnostního auditu a
hlídá, aby se daná chyba nevrátila:

- H1: ``setup_hook`` nesmí spolknout selhání načtení cogu,
- H2 (duplicitní background tasky po reconnectu) hlídá ``tests/test_bot.py``
  (``StartupOnceTests``) – main to řeší příznakem ``_startup_done``,
- H3: ``/sendht3`` je admin-only,
- M1: neúspěšné smazání obsolete guild příkazu se nehlásí jako úspěch,
- M2: neplatná hodnota proměnné prostředí se ozloguje, ne spadne potichu,
- M3: ``/openq`` nesmí tise spadnout na neodeslaném panelu po purge,
- L8: ORM model ``TesterCredit`` se nesmí sbírat jako pytest test.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import discord

import bot as bot_module
import cogs._shared
import cogs.ht3
import cogs.queues
import config
import utils
from db.models.tester_credits import TesterCredit


class _NoMessages:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _fake_http_error():
    return discord.HTTPException(mock.MagicMock(), "boom")


class TestSetupHookFailFast:
    def test_all_extensions_are_attempted(self):
        expected = {
            "cogs.queues",
            "cogs.results",
            "cogs.ht3",
            "cogs.tournaments",
            "cogs.kits",
            "cogs.roles",
            "cogs.sync",
            "cogs.topresult",
            "cogs.info",
            "cogs.edituser",
            "cogs.link",
            "cogs.retire",
        }
        bot = bot_module.DachshundTiersBot()
        loaded: list[str] = []

        async def _load(ext: str):
            loaded.append(ext)

        with mock.patch.object(bot, "load_extension", mock.AsyncMock(side_effect=_load)):
            asyncio.run(bot.setup_hook())

        assert set(loaded) == expected

    def test_failed_extension_raises_instead_of_silent_ready(self):
        bot = bot_module.DachshundTiersBot()

        async def _load(ext: str):
            if ext == "cogs.queues":
                raise RuntimeError("import boom")

        with mock.patch.object(bot, "load_extension", mock.AsyncMock(side_effect=_load)):
            try:
                asyncio.run(bot.setup_hook())
            except RuntimeError as err:
                assert "cogs.queues" in str(err)
            else:
                raise AssertionError("setup_hook měl selhat, ale neselhal")


class TestSendHt3Gate:
    def test_non_admin_is_refused_before_anything_is_sent(self):
        cog = cogs.ht3.HT3(mock.Mock())
        interaction = mock.Mock()
        interaction.user = SimpleNamespace(id=1, roles=[])
        interaction.response.send_message = mock.AsyncMock()

        with mock.patch.object(cogs.ht3, "has_admin_role", return_value=False):
            asyncio.run(cog.sendht3.callback(cog, interaction))

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
        cog.bot.get_channel.assert_not_called()


class TestObsoleteCommandRemoval:
    def test_failed_deletion_is_not_reported_as_removed(self):
        stale = SimpleNamespace(id=1, name="stary")
        broken = SimpleNamespace(id=2, name="zly")
        tree = mock.Mock()
        tree.fetch_commands = mock.AsyncMock(return_value=[stale, broken])
        tree._http.delete_guild_command = mock.AsyncMock(
            side_effect=[None, _fake_http_error()]
        )

        removed = asyncio.run(
            bot_module._remove_obsolete_guild_commands(
                tree, guild=discord.Object(id=1), intended=set()
            )
        )

        assert removed == ["stary"]


class TestConfigEnvLogging:
    def test_invalid_int_env_is_logged(self, monkeypatch):
        monkeypatch.setenv("GUILD_ID", "not-an-int")
        with mock.patch.object(config.log, "error") as log_error:
            assert config._int_env("GUILD_ID", 0) == 0
        assert log_error.called
        assert "GUILD_ID" in log_error.call_args.args

    def test_invalid_json_env_is_logged(self, monkeypatch):
        monkeypatch.setenv("QUEUE_CHANNELS_JSON", "{broken")
        with mock.patch.object(config.log, "error") as log_error:
            assert config._dict_env("QUEUE_CHANNELS_JSON", {"a": 1}) == {"a": 1}
        assert log_error.called

    def test_invalid_list_item_is_logged(self, monkeypatch):
        monkeypatch.setenv("TESTER_ROLE_IDS", "1,x,3")
        with mock.patch.object(config.log, "error") as log_error:
            assert config._int_list_env("TESTER_ROLE_IDS") == [1, 3]
        assert log_error.called


class TestOpenQueuePanelFailure:
    def test_panel_send_failure_is_reported_and_not_silently_lost(self):
        cog = cogs.queues.Queues(mock.Mock())
        cog.bot.db_session_factory = None

        interaction = mock.Mock()
        interaction.user = SimpleNamespace(id=1, display_name="tester")
        interaction.response.send_message = mock.AsyncMock()

        kit_channel = mock.Mock()
        kit_channel.history = mock.Mock(return_value=_NoMessages())
        kit_channel.send = mock.AsyncMock(
            side_effect=discord.Forbidden(mock.MagicMock(), "no perms")
        )
        interaction.guild.get_channel = mock.Mock(return_value=kit_channel)

        set_panel = mock.AsyncMock()

        with (
            mock.patch.object(cogs.queues, "has_tester_role", return_value=True),
            mock.patch.object(
                cogs.queues,
                "open_queue",
                mock.AsyncMock(return_value=("ok", {"testers": []})),
            ),
            mock.patch.object(
                cogs.queues, "get_queue_channel_id", mock.AsyncMock(return_value=555)
            ),
            mock.patch.object(
                cogs.queues, "list_queue_entries", mock.AsyncMock(return_value=[])
            ),
            mock.patch.object(cogs.queues, "create_queue_embed", mock.Mock(return_value=mock.Mock())),
            mock.patch.object(cogs.queues, "set_queue_panel", set_panel),
        ):
            asyncio.run(cog.openq.callback(cog, interaction, kit="randompot"))

        interaction.response.send_message.assert_awaited_once()
        assert "nepodařilo poslat" in interaction.response.send_message.await_args.args[0]
        set_panel.assert_not_awaited()


def test_tester_credit_model_is_not_collected_by_pytest():
    assert TesterCredit.__test__ is False


class TestSwallowedFallbacksNowLogged:
    def test_guild_members_logs_and_falls_back_to_cache(self):
        member = SimpleNamespace(bot=False, id=1)
        guild = mock.Mock()
        guild.members = [member]

        class _RaisingFlatten:
            async def flatten(self):
                raise RuntimeError("no members intent")

        guild.fetch_members = mock.Mock(return_value=_RaisingFlatten())

        with mock.patch.object(cogs._shared.log, "warning") as log_warning:
            result = asyncio.run(cogs._shared.guild_members(guild))

        assert result == [member]
        assert log_warning.called

    def test_kit_autocomplete_logs_and_uses_default_kits(self, monkeypatch):
        interaction = mock.Mock()
        interaction.client = None
        monkeypatch.setattr(
            "services.kit_catalog.get_kits",
            mock.AsyncMock(side_effect=RuntimeError("db down")),
        )

        with mock.patch.object(utils._log, "warning") as log_warning:
            choices = asyncio.run(utils.kit_autocomplete(interaction, "anchor"))

        assert log_warning.called
        assert any(choice.name == "AnchorPvP" for choice in choices)

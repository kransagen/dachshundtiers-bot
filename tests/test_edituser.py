"""Testy /edituser – centrální admin editor hráče (bez discord.py).

Bývalé JSON-mode testy (``apply_player_edit``/``execute_player_edit`` nad
``players.json`` přes ``services.store``, ``data/edituser_log.json``, staré
cooldown pomocné ``cooldown_snapshot``/``ht3_cooldown_remaining``) jsou PRYČ
spolu s JSON režimem. Produkční chování ``apply_player_edit`` /
``execute_player_edit`` / ``get_edituser_log`` nad PostgreSQL (identita, tier
invarianty, cooldowny, audit ve stejné transakci, role/web best effort) je
pokryté ``tests/test_edituser_db.py``.

Tenhle soubor drží jen:
- čistou logiku nezávislou na úložišti (identita, tier invarianty, role plán),
- cog-plumbing testy (admin gate, view navigace, stale check mezi náhledem
  a potvrzením – porovnání s kanonickou podobou z PostgreSQL přes
  ``_find_player``),
- /linkdiscord (PostgreSQL-first, JSON = export-only),
- regresní testy, že JSON fallback se do services/edituser.py nevrátil.
"""

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
import storage
from cogs.edituser import (
    ConfirmEditView,
    EditUser,
    HistoryView,
    PlayerEditorView,
    TierKitSelectView,
)
from services import edituser as eu
from services import permissions
from services.player_identity import CLAIM_UNCHANGED, PlayerIdentityConflict
from services.playersync import make_member

PLAYER_ID = "111111111111111111"
OTHER_ID = "222222222222222222"
NEW_ID = "333333333333333333"

NOW = 1_700_000_000_000
QUEUE_CD = 4 * 24 * 60 * 60 * 1000  # 4 d
HT3_CD = 7 * 24 * 60 * 60 * 1000  # 7 d

READY = [
    {
        "username": "AliceMC",
        "discordId": PLAYER_ID,
        "modes": {"randompot": "HT2"},
        "history": {
            "randompot": [{"date": "01.01.2026", "tier": "LT3"}]
        },
    },
    {
        "username": "BobMC",
        "discordId": OTHER_ID,
        "modes": {},
        "history": {},
    },
]


def _players():
    """Hluboká kopie výchozí databáze hráčů (vstup testů se nemutuje)."""
    return json.loads(json.dumps(READY))


def _canned_player(**overrides):
    """Kanonická podoba hráče, jakou vrací ``_find_player`` (build_player_shape)."""
    base = {
        "username": "AliceMC",
        "discordId": PLAYER_ID,
        "modes": {"randompot": "HT2"},
        "history": {"randompot": [{"date": "01.01.2026", "tier": "LT3"}]},
    }
    base.update(overrides)
    return base


def _write(name, data):
    storage.save_data(name, data)


def _admin_member(rid: int = 999):
    return SimpleNamespace(
        roles=[SimpleNamespace(id=rid, name="Vedení")],
        guild_permissions=SimpleNamespace(administrator=False),
    )


def _plain_member():
    return SimpleNamespace(
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
    return inter


def _kit_roles_map():
    return {"randompot": {"HT3": "101", "HT2": "102", "RLT2": "105"}}


class EditUserServiceTests(unittest.TestCase):
    """Čisté funkce: identita, tier invarianty, role plán."""

    # ------------------------------------------------------------------
    # IGN
    # ------------------------------------------------------------------
    def test_ign_change_renames_and_preserves_data(self):
        players = _players()
        new_players, target, outcome = eu.change_player_ign(
            players, {"discordId": PLAYER_ID}, "AliceNew"
        )
        self.assertEqual(outcome, "renamed")
        self.assertEqual(target["username"], "AliceNew")
        self.assertEqual(target["discordId"], PLAYER_ID)
        self.assertEqual(target["modes"], {"randompot": "HT2"})
        self.assertEqual(
            target["history"], {"randompot": [{"date": "01.01.2026", "tier": "LT3"}]}
        )
        # vstup se nemutoval
        self.assertEqual(players[0]["username"], "AliceMC")

    def test_ign_conflict_rejected(self):
        players = _players()
        with self.assertRaises(PlayerIdentityConflict):
            eu.change_player_ign(
                players, {"discordId": PLAYER_ID}, "BobMC"  # patří jinému ID
            )
        # po konfliktu zůstávají oba záznamy beze změny
        self.assertEqual(
            [p["username"] for p in players], ["AliceMC", "BobMC"]
        )

    def test_ign_same_value_unchanged(self):
        players = _players()
        _new, target, outcome = eu.change_player_ign(
            players, {"discordId": PLAYER_ID}, "AliceMC"
        )
        self.assertEqual(outcome, CLAIM_UNCHANGED)
        self.assertEqual(target["username"], "AliceMC")

    # ------------------------------------------------------------------
    # Discord ID
    # ------------------------------------------------------------------
    def test_discord_change_preserves_everything(self):
        players = _players()
        new_players, target, outcome = eu.change_player_discord(
            players, {"discordId": PLAYER_ID}, NEW_ID
        )
        self.assertEqual(outcome, "changed")
        self.assertEqual(target["discordId"], NEW_ID)
        self.assertNotIn(PLAYER_ID, [p["discordId"] for p in new_players])
        self.assertEqual(target["username"], "AliceMC")
        self.assertEqual(target["modes"], {"randompot": "HT2"})
        self.assertEqual(len(new_players), 2)  # NIKDY nevznikne nový hráč

    def test_discord_conflict_rejected(self):
        players = _players()
        with self.assertRaises(PlayerIdentityConflict):
            eu.change_player_discord(
                players, {"discordId": PLAYER_ID}, OTHER_ID  # už patří BobMC
            )
        self.assertEqual(players[0]["discordId"], PLAYER_ID)

    def test_discord_short_id_rejected(self):
        players = _players()
        with self.assertRaises(PlayerIdentityConflict):
            eu.change_player_discord(players, {"discordId": PLAYER_ID}, "12345")

    def test_discord_same_id_unchanged(self):
        players = _players()
        _new, target, outcome = eu.change_player_discord(
            players, {"discordId": PLAYER_ID}, PLAYER_ID
        )
        self.assertEqual(outcome, CLAIM_UNCHANGED)

    # ------------------------------------------------------------------
    # Tier invarianty
    # ------------------------------------------------------------------
    def test_current_tier_change_updates_modes_only(self):
        players = _players()
        new_players, target, old, outcome = eu.change_kit_tier(
            players, {"discordId": PLAYER_ID}, "randompot", "HT3", retired=False
        )
        self.assertEqual(outcome, eu.OUTCOME_CHANGED)
        self.assertEqual(old, "HT2")
        self.assertEqual(target["modes"], {"randompot": "HT3"})
        self.assertEqual(  # historie se NIKDY nemění
            target["history"], {"randompot": [{"date": "01.01.2026", "tier": "LT3"}]}
        )
        self.assertEqual(len(new_players), 2)

    def test_retire_exact_same_value_allowed(self):
        players = _players()
        _new, target, _old, outcome = eu.change_kit_tier(
            players, {"discordId": PLAYER_ID}, "randompot", "RHT2", retired=True
        )
        self.assertEqual(outcome, eu.OUTCOME_CHANGED)
        self.assertEqual(target["modes"], {"randompot": "RHT2"})

    def test_retired_over_other_current_rejected(self):
        players = _players()  # Alice má HT2
        with self.assertRaises(eu.InvalidTierEdit):
            eu.change_kit_tier(
                players, {"discordId": PLAYER_ID}, "randompot", "RHT3", retired=True
            )
        self.assertEqual(players[0]["modes"]["randompot"], "HT2")

    def test_current_over_retired_rejected(self):
        players = _players()
        players[0]["modes"] = {"randompot": "RHT3"}  # archivovaná historie
        with self.assertRaises(eu.InvalidTierEdit):
            eu.change_kit_tier(
                players, {"discordId": PLAYER_ID}, "randompot", "HT3", retired=False
            )

    def test_retired_to_retired_allowed(self):
        players = _players()
        players[0]["modes"] = {"randompot": "RHT3"}
        _new, target, _old, outcome = eu.change_kit_tier(
            players, {"discordId": PLAYER_ID}, "randompot", "RLT2", retired=True
        )
        self.assertEqual(outcome, eu.OUTCOME_CHANGED)
        self.assertEqual(target["modes"], {"randompot": "RLT2"})

    def test_same_tier_unchanged(self):
        players = _players()
        _new, _target, _old, outcome = eu.change_kit_tier(
            players, {"discordId": PLAYER_ID}, "randompot", "HT2", retired=False
        )
        self.assertEqual(outcome, eu.OUTCOME_UNCHANGED)

    def test_display_case_kit_writes_existing_key(self):
        """Bývalý bug: /result píše display-case klíče ("RandomPot"),
        editor by lowercase klíčem vytvořil duplicitu."""
        players = json.loads(json.dumps(READY))
        players[0]["modes"] = {"RandomPot": "HT2"}  # /result styl zápisu
        _new, target, _old, outcome = eu.change_kit_tier(
            players, {"discordId": PLAYER_ID}, "randompot", "HT3", retired=False
        )
        self.assertEqual(outcome, eu.OUTCOME_CHANGED)
        self.assertEqual(list(target["modes"].keys()), ["RandomPot"])
        self.assertEqual(target["modes"]["RandomPot"], "HT3")

    def test_tier_choices_no_free_text(self):
        self.assertIn("HT3", eu.current_tier_choices())
        self.assertNotIn("LT3E", eu.current_tier_choices())  # virtuální status
        self.assertIn("RHT3", eu.retired_tier_choices())

    # ------------------------------------------------------------------
    # plan_role_sync (RoleSyncService, jeden kit)
    # ------------------------------------------------------------------
    def _member(self, rid="111", roles=None):
        return make_member(rid, "AliceMC", roles or set())

    def test_plan_builds_actions_for_current_tier(self):
        player = {"username": "AliceMC", "discordId": PLAYER_ID,
                  "modes": {"randompot": "HT3"}}
        plan = eu.plan_role_sync(
            player=player,
            member=self._member(roles={"102"}),  # už má HT2 roli
            roles_map=_kit_roles_map(),
            kit_key="randompot",
            kit_display={"randompot": "RandomPot"},
        )
        self.assertTrue(plan["synced"])
        self.assertEqual(plan["note"], "")
        ops = {(a["op"], a["role_id"]) for a in plan["actions"]}
        self.assertIn(("add", "101"), ops)    # nová HT3 role
        self.assertIn(("remove", "102"), ops)  # stará HT2 role

    def test_plan_skips_retired_tier(self):
        player = {"username": "AliceMC", "discordId": PLAYER_ID,
                  "modes": {"randompot": "RHT3"}}
        plan = eu.plan_role_sync(
            player=player, member=self._member(roles={"101"}),
            roles_map=_kit_roles_map(), kit_key="randompot",
        )
        self.assertFalse(plan["synced"])
        self.assertIn("retired", plan["note"])
        self.assertEqual(plan["actions"], [])

    def test_plan_skips_missing_member(self):
        player = {"username": "AliceMC", "discordId": PLAYER_ID,
                  "modes": {"randompot": "HT3"}}
        plan = eu.plan_role_sync(
            player=player, member=None,
            roles_map=_kit_roles_map(), kit_key="randompot",
        )
        self.assertFalse(plan["synced"])
        self.assertIn("není na serveru", plan["note"])

    def test_plan_skips_unmapped_tier(self):
        player = {"username": "AliceMC", "discordId": PLAYER_ID,
                  "modes": {"randompot": "S"}}  # S nemá namapovanou roli
        plan = eu.plan_role_sync(
            player=player, member=self._member(),
            roles_map=_kit_roles_map(), kit_key="randompot",
        )
        self.assertFalse(plan["synced"])
        self.assertIn("nemá namapovanou roli", plan["note"])

    def test_plan_resolves_display_case_mode_key(self):
        player = {"username": "AliceMC", "discordId": PLAYER_ID,
                  "modes": {"RandomPot": "HT3"}}
        plan = eu.plan_role_sync(
            player=player, member=self._member(),
            roles_map=_kit_roles_map(), kit_key="randompot",
            kit_display={"randompot": "RandomPot"},
        )
        self.assertTrue(plan["synced"])
        self.assertEqual([a["role_id"] for a in plan["actions"]], ["101"])

    def test_plan_without_tier_is_inactive(self):
        plan = eu.plan_role_sync(
            player={"username": "AliceMC", "discordId": PLAYER_ID, "modes": {}},
            member=self._member(), roles_map=_kit_roles_map(), kit_key="randompot",
        )
        self.assertFalse(plan["synced"])
        self.assertIn("žádný tier", plan["note"])

    # ------------------------------------------------------------------
    # Cooldown pomocná funkce (zbývající – staré snapshouty jsou pryč)
    # ------------------------------------------------------------------
    def test_format_duration(self):
        self.assertEqual(eu.format_duration(None), "žádný")
        self.assertEqual(eu.format_duration(0), "žádný")
        self.assertEqual(eu.format_duration(HT3_CD), "7d 0h 0m")
        self.assertEqual(eu.format_duration(2 * 60 * 60 * 1000), "2h 0m")


class NoJsonModeTests(unittest.TestCase):
    """services/edituser.py už nemá JSON režim – bez PostgreSQL odmítne."""

    def test_apply_player_edit_refuses_without_session_factory(self):
        async def main():
            with self.assertRaisesRegex(RuntimeError, "players.json se už nepoužívá"):
                await eu.apply_player_edit(
                    player_id=PLAYER_ID,
                    edit={"field": "ign", "new_value": "AliceNew"},
                    actor_id=7, actor_name="Admin", now=NOW,
                    queue_cooldown_ms=QUEUE_CD, ht3_cooldown_ms=HT3_CD,
                )
        asyncio.run(main())

    def test_execute_player_edit_refuses_without_session_factory(self):
        async def main():
            with self.assertRaisesRegex(RuntimeError, "players.json se už nepoužívá"):
                await eu.execute_player_edit(
                    player_id=PLAYER_ID,
                    edit={"field": "ign", "new_value": "AliceNew"},
                    actor_id=7, actor_name="Admin", now=NOW,
                    queue_cooldown_ms=QUEUE_CD, ht3_cooldown_ms=HT3_CD,
                )
        asyncio.run(main())

    def test_get_edituser_log_refuses_without_session_factory(self):
        async def main():
            with self.assertRaisesRegex(RuntimeError, "edituser_log.json se už nepoužívá"):
                await eu.get_edituser_log(session_factory=None)
        asyncio.run(main())

    def test_json_fallback_absent_in_source(self):
        """Zdrojový regresní test: JSON cesta z services/edituser.py je pryč."""
        import inspect

        src = inspect.getsource(eu)
        self.assertNotIn("load_data(", src)
        self.assertNotIn("save_data(", src)
        self.assertNotIn("if session_factory is not None:", src)
        self.assertNotIn("EDITUSER_LOG_FILE", src)
        self.assertNotIn("def cooldown_snapshot", src)
        self.assertNotIn("def ht3_cooldown_remaining", src)


class CogPermissionTests(unittest.TestCase):
    """/edituser a jeho view – POUZE admini (has_admin_role / ADMIN_ROLE_IDS)."""

    def test_command_denied_for_non_admin(self):
        cog = EditUser.__new__(EditUser)
        inter = _interaction(user=_plain_member())
        player = SimpleNamespace(id=PLAYER_ID)

        async def main():
            await EditUser.edituser.callback(cog, inter, player)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()

    def test_command_denied_outside_guild(self):
        cog = EditUser.__new__(EditUser)
        inter = _interaction(user=_admin_member())
        inter.guild = None  # mimo server → i admin je odmítnut
        player = SimpleNamespace(id=PLAYER_ID)

        async def main():
            await EditUser.edituser.callback(cog, inter, player)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze na serveru.", ephemeral=True
        )

    def test_command_accepts_admin_role(self):
        with mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999]):
            cog = EditUser.__new__(EditUser)
            inter = _interaction(user=_admin_member(rid=999))
            player = SimpleNamespace(id=123456789012345678)

            async def main():
                # Hráč se hledá v PostgreSQL (_find_player) – tady neexistuje.
                with mock.patch(
                    "cogs.edituser._find_player",
                    new=mock.AsyncMock(return_value=None),
                ):
                    await EditUser.edituser.callback(cog, inter, player)

            asyncio.run(main())
            inter.response.defer.assert_awaited_once()  # prošlo admin gate
            inter.followup.send.assert_awaited_once()  # hráč nenalezen
            msg = inter.followup.send.await_args.kwargs.get("ephemeral", False)
            self.assertTrue(msg)

    def test_view_button_denied_for_non_admin(self):
        cog = EditUser.__new__(EditUser)
        view = PlayerEditorView(cog=cog, player_id=PLAYER_ID)
        inter = _interaction(user=_plain_member())

        async def main():
            await PlayerEditorView.on_discord(view, inter, None)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )

    def test_admin_gate_in_second_party_view(self):
        """I podviewy (tier select) kontrolují admina – nejen hlavní menu."""
        from cogs.edituser import TierSelectView

        cog = EditUser.__new__(EditUser)
        view = TierSelectView(cog=cog, player_id=PLAYER_ID, kit_key="randompot")
        inter = _interaction(user=_plain_member(), guild=mock.MagicMock())
        inter.data = {"values": ["HT3"]}
        inter.response.send_message = mock.AsyncMock()

        async def main():
            await TierSelectView.on_tier(view, inter)

        asyncio.run(main())
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )


class StaleCheckTests(unittest.TestCase):
    """Stale check mezi náhledem a potvrzením (ConfirmEditView._stale_check).

    Porovnává se s kanonickou podobou hráče z PostgreSQL (přes ``_find_player``
    → ``build_player_shape``) – players.json se už nikdy nečte.
    """

    def _view(self, stale):
        return ConfirmEditView(
            cog=EditUser.__new__(EditUser),
            payload={"player_id": PLAYER_ID, "stale": stale},
        )

    def _check(self, stale, player=None):
        async def main():
            with mock.patch(
                "cogs.edituser._find_player",
                new=mock.AsyncMock(return_value=player),
            ):
                return await self._view(stale)._stale_check()
        return asyncio.run(main())

    def test_stale_ok_when_unchanged(self):
        player = _canned_player()
        self.assertIsNone(self._check({"field": "tier", "kit": "randompot", "old_value": "HT2"}, player=player))
        self.assertIsNone(self._check({"field": "ign", "old_value": "AliceMC"}, player=player))
        self.assertIsNone(self._check({"field": "discord_id", "old_value": PLAYER_ID}, player=player))

    def test_tier_changed_flagged_stale(self):
        player = _canned_player(modes={"randompot": "HT3"})
        msg = self._check({"field": "tier", "kit": "randompot", "old_value": "HT2"}, player=player)
        self.assertIn("změnil", msg)
        self.assertIn("HT3", msg)

    def test_ign_changed_flagged_stale(self):
        player = _canned_player(username="AliceNew")
        msg = self._check({"field": "ign", "old_value": "AliceMC"}, player=player)
        self.assertIn("změnilo", msg)

    def test_discord_changed_flagged_stale(self):
        # Po změně Discord ID už hráč není dohledatelný pod starým ID → stale
        # → nic se neaplikuje (bezpečný abort i v situaci, kdy hráč existuje
        # pod jiným ID). _find_player podle starého ID vrátí None.
        msg = self._check({"field": "discord_id", "old_value": PLAYER_ID}, player=None)
        self.assertIn("není", msg)

    def test_missing_player_flagged_stale(self):
        msg = self._check({"field": "ign", "old_value": "AliceMC"}, player=None)
        self.assertIn("není", msg)

    def test_display_case_mode_key_matches_stale(self):
        """Stale check tieru musí najít display-case klíč ("RandomPot")."""
        player = _canned_player(modes={"RandomPot": "HT2"})
        msg = self._check({"field": "tier", "kit": "randompot", "old_value": "HT2"}, player=player)
        self.assertIsNone(msg)


class ConfirmViewMechanicsTests(unittest.TestCase):
    """View mechanika /edituser: tlačítka → modály/selecty → potvrzení.

    Klíčový regresní test: ``on_confirm`` volá ``execute_player_edit`` – dřív
    chyběl import (NameError → crash tlačítka „Potvrdit a aplikovat"
    v produkci). Teď se potvrzení aplikuje a zpráva se přepíše reportem.
    """

    def _inter(self):
        inter = _interaction(user=_admin_member())
        inter.user.id = 999
        inter.message = mock.MagicMock()
        inter.edit_original_response = mock.AsyncMock()
        return inter

    def _payload(self, **overrides):
        payload = {
            "player_id": PLAYER_ID,
            "title": "Změna Discord ID",
            "lines": ["`old` → `new`"],
            "edit": {"field": "discord_id", "from": PLAYER_ID, "to": NEW_ID},
            "stale": None,
        }
        payload.update(overrides)
        return payload

    def test_confirm_calls_execute_player_edit_and_finishes(self):
        """Regrese itemu 1: potvrzení aplikuje změnu a nepřepíše stav."""
        cog = EditUser.__new__(EditUser)
        view = ConfirmEditView(cog=cog, payload=self._payload())
        inter = self._inter()

        report = {
            "status": eu.STATUS_SUCCESS,
            "message": "Změny aplikované.",
            "db": {"status": "ok", "message": "Uloženo"},
            "roles": {"actions": []},
            "web": {"status": "ok", "pushed": False},
            "errors": [],
        }

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser.execute_player_edit",
                     new=mock.AsyncMock(return_value=report),
                 ) as exe:
                await ConfirmEditView.on_confirm(view, inter, None)
            return exe

        exe = asyncio.run(main())
        exe.assert_awaited_once()
        call_kwargs = exe.await_args.kwargs
        self.assertEqual(call_kwargs["player_id"], PLAYER_ID)
        self.assertEqual(call_kwargs["edit"], self._payload()["edit"])
        self.assertTrue(view.finished)
        # report se přepíše místo konfirmace + duplikát jako ephemeral message
        inter.edit_original_response.assert_awaited_once()
        inter.followup.send.assert_awaited_once()
        embed = inter.followup.send.await_args.kwargs["embed"]
        self.assertIn(eu.STATUS_SUCCESS, embed.title)

    def test_confirm_after_finished_short_circuits(self):
        cog = EditUser.__new__(EditUser)
        view = ConfirmEditView(cog=cog, payload=self._payload())
        view.finished = True
        inter = self._inter()

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser.execute_player_edit",
                     new=mock.AsyncMock(return_value={}),
                 ) as exe:
                await ConfirmEditView.on_confirm(view, inter, None)
            return exe

        exe = asyncio.run(main())
        exe.assert_not_awaited()  # nic se neaplikuje podruhé
        inter.response.send_message.assert_awaited_once_with(
            "✅ Změny už byly aplikované.", ephemeral=True
        )

    def test_confirm_with_stale_aborts_safely(self):
        """Mezi náhledem a potvrzením se stav změnil → NIC se neaplikuje."""
        cog = EditUser.__new__(EditUser)
        view = ConfirmEditView(
            cog=cog,
            payload=self._payload(stale={"field": "discord_id", "old_value": "NOPE"}),
        )
        inter = self._inter()

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser._find_player",
                     new=mock.AsyncMock(return_value=_canned_player()),
                 ), \
                 mock.patch(
                     "cogs.edituser.execute_player_edit",
                     new=mock.AsyncMock(return_value={}),
                 ) as exe:
                await ConfirmEditView.on_confirm(view, inter, None)
            return exe

        exe = asyncio.run(main())
        exe.assert_not_awaited()
        self.assertTrue(view.finished)
        inter.followup.send.assert_awaited_once()

    def test_cancel_back_to_main_returns_to_menu(self):
        cog = EditUser.__new__(EditUser)
        view = ConfirmEditView(
            cog=cog, payload=self._payload(back_to_main=True)
        )
        inter = self._inter()

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser._find_player",
                     new=mock.AsyncMock(return_value=_canned_player()),
                 ):
                await ConfirmEditView.on_cancel(view, inter, None)

        asyncio.run(main())
        self.assertTrue(view.finished)
        # vrátilo se do hlavního menu (překreslení) – ne zavření
        inter.edit_original_response.assert_awaited_once()
        _, kwargs = inter.edit_original_response.await_args
        self.assertIsInstance(kwargs["view"], PlayerEditorView)


class EditViewMechanicsTests(unittest.TestCase):
    """Hlavní menu /edituser: admin gate + překreslení podviewů (swap)."""

    def _inter(self):
        inter = _interaction(user=_admin_member())
        inter.message = mock.MagicMock()
        inter.edit_original_response = mock.AsyncMock()
        return inter

    def test_close_button_removes_view(self):
        cog = EditUser.__new__(EditUser)
        view = PlayerEditorView(cog=cog, player_id=PLAYER_ID)
        inter = self._inter()

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True):
                await PlayerEditorView.on_close(view, inter, None)

        asyncio.run(main())
        inter.edit_original_response.assert_awaited_once()
        self.assertEqual(inter.edit_original_response.await_args.kwargs["view"], None)
        inter.response.defer.assert_awaited_once()
        inter.followup.send.assert_awaited_once()

    def test_tiers_button_swaps_to_kit_select(self):
        cog = EditUser.__new__(EditUser)
        view = PlayerEditorView(cog=cog, player_id=PLAYER_ID)
        inter = self._inter()

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser.get_kits", new=mock.AsyncMock(return_value=[])
                 ), \
                 mock.patch(
                     "cogs.edituser._find_player",
                     new=mock.AsyncMock(return_value=_canned_player()),
                 ):
                await PlayerEditorView.on_tiers(view, inter, None)

        asyncio.run(main())
        inter.edit_original_response.assert_awaited_once()
        self.assertIsInstance(
            inter.edit_original_response.await_args.kwargs["view"], TierKitSelectView
        )

    def test_history_button_swaps_to_history(self):
        cog = EditUser.__new__(EditUser)
        view = PlayerEditorView(cog=cog, player_id=PLAYER_ID)
        inter = self._inter()

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser._find_player",
                     new=mock.AsyncMock(return_value=_canned_player()),
                 ):
                await PlayerEditorView.on_history(view, inter, None)

        asyncio.run(main())
        inter.edit_original_response.assert_awaited_once()
        self.assertIsInstance(
            inter.edit_original_response.await_args.kwargs["view"], HistoryView
        )

    def test_swap_followup_when_edit_fails(self):
        """Selhání překreslení (HTTPException) → fallback přes followup."""
        cog = EditUser.__new__(EditUser)
        view = PlayerEditorView(cog=cog, player_id=PLAYER_ID)
        inter = self._inter()
        inter.edit_original_response = mock.AsyncMock(
            side_effect=discord.HTTPException(mock.MagicMock(), "boom")
        )

        async def main():
            with mock.patch("cogs.edituser.has_admin_role", return_value=True), \
                 mock.patch(
                     "cogs.edituser.get_kits", new=mock.AsyncMock(return_value=[])
                 ), \
                 mock.patch(
                     "cogs.edituser._find_player",
                     new=mock.AsyncMock(return_value=_canned_player()),
                 ):
                await PlayerEditorView.on_tiers(view, inter, None)

        asyncio.run(main())
        inter.followup.send.assert_awaited_once()
        self.assertIsInstance(
            inter.followup.send.await_args.kwargs["view"], TierKitSelectView
        )


class TestLinkDiscordCog(unittest.TestCase):
    """/linkdiscord po Phase D cutoveru: PostgreSQL-first, JSON export-only."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        _write("players.json", _players())
        self.cog = EditUser.__new__(EditUser)
        self.cog.bot = SimpleNamespace(db_session_factory=None)
        self._admin_roles = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        self._admin_roles.start()
        self.addCleanup(self._admin_roles.stop)

    def _claim(self, inter, player_id, ign):
        player = SimpleNamespace(id=player_id)
        async def main():
            await EditUser.linkdiscord.callback(self.cog, inter, player, ign)
        asyncio.run(main())
        return inter

    def _players(self):
        return storage.load_data("players.json", [])

    def _succeed(self, status):
        from db.repositories.players import PlayerRepository

        class FakeTransaction:
            def __init__(self, factory):
                pass

            async def __aenter__(self):
                return object()

            async def __aexit__(self, *args):
                return False

        claim_mock = mock.AsyncMock(return_value=(status, object()))
        self.cog.bot = SimpleNamespace(db_session_factory=object())
        return mock.patch(
            "db.services.session.transaction",
            lambda sf: FakeTransaction(sf),
        ), mock.patch.object(
            PlayerRepository, "claim_discord_id", new=claim_mock
        ), claim_mock

    def test_non_admin_denied(self):
        inter = self._claim(_interaction(user=_plain_member()), NEW_ID, "CarlMC")
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze pro administrátory.", ephemeral=True
        )
        inter.response.defer.assert_not_awaited()

    def test_outside_guild_denied(self):
        inter = _interaction(user=_admin_member())
        inter.guild = None
        inter = self._claim(inter, NEW_ID, "CarlMC")
        inter.response.send_message.assert_awaited_once_with(
            "❌ Pouze na serveru.", ephemeral=True
        )

    def test_no_db_fails_loudly_no_json_claim(self):
        players_before = self._players()
        inter = self._claim(_interaction(user=_admin_member()), NEW_ID, "CarlMC")
        msg = inter.followup.send.await_args.args[0]
        self.assertIn("PostgreSQL-first", msg)
        self.assertIn("no silent fallback", msg)
        self.assertEqual(self._players(), players_before)

    def _link(self, *, outcome=None, error=None):
        from services.player_link import LinkOutcome

        self.cog.bot = SimpleNamespace(db_session_factory=object())
        link = mock.AsyncMock(
            return_value=outcome or LinkOutcome(status="created", ign="CarlMC", player_id=1),
            side_effect=error,
        )
        admin = _admin_member()
        admin.id = 5
        inter = _interaction(user=admin)
        inter.guild.get_member = mock.MagicMock(return_value=None)
        with mock.patch("services.player_link.link_ign", new=link):
            self._claim(inter, NEW_ID, "CarlMC")
        return inter, link

    def test_db_link_uses_link_service(self):
        inter, link = self._link()
        self.assertEqual(link.await_args.args, (NEW_ID, "CarlMC"))
        self.assertIsNotNone(link.await_args.kwargs["actor_id"])
        embed = inter.followup.send.await_args.kwargs["embed"]
        self.assertIn("Vytvořen nový hráč", embed.description)

    def test_link_refusal_is_reported(self):
        from services.player_link import REFUSE_IGN_TAKEN, LinkRefused

        inter, _ = self._link(error=LinkRefused(REFUSE_IGN_TAKEN, ign="CarlMC"))
        self.assertIn("už je propojené", inter.followup.send.await_args.args[0])

if __name__ == "__main__":
    unittest.main()
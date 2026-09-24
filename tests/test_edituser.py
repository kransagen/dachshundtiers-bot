"""Testy /edituser – centrální admin editor hráče.

Pokrývají čistou službu (``services/edituser.py``), orchestrátor
(``execute_player_edit``: DB → Discord role → web, best effort) i kog
(``cogs/edituser.py``: oprávnění adminů + stale check).

Scénáře z požadavku:
- změna IGN (přejmenování, konflikt, idempotence),
- změna Discord ID (primární identita, migrace klíčů, konflikt, NIKDY neslučovat),
- tier úpravy (aktuální / retired invarianty, žádný free-text, historie se nemění),
- Discord role (plan_role_sync + aplikace; selhání API → PARTIAL SUCCESS),
- web sync (falška push_web; selhání → PARTIAL SUCCESS, kanonická DB se nemění
  zpět / neztrácí),
- cooldowny (clear/set waitlist + HT3 per kit, staré výpočty),
- poškozený JSON → DataCorruptionError → bezpečný abort (žádný overwrite),
- oprávnění (pouze admini),
- audit (edituser_log.json, pole + idempotence),
- stale check mezi náhledem a potvrzením.
"""

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import storage
from cogs.edituser import ConfirmEditView, EditUser, PlayerEditorView
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
    """Čisté funkce: identita, tier invarianty, role plán, cooldown pomocné."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

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
    # Cooldown pomocné funkce
    # ------------------------------------------------------------------
    def test_format_duration(self):
        self.assertEqual(eu.format_duration(None), "žádný")
        self.assertEqual(eu.format_duration(0), "žádný")
        self.assertEqual(eu.format_duration(HT3_CD), "7d 0h 0m")
        self.assertEqual(eu.format_duration(2 * 60 * 60 * 1000), "2h 0m")

    def test_cooldown_snapshot(self):
        cooldowns = {PLAYER_ID: NOW - 24 * 60 * 60 * 1000}  # waitlist (4 d od teď → 3 d zbývá)
        ht3 = {PLAYER_ID: {"randompot": NOW + HT3_CD}}
        snap = eu.cooldown_snapshot(cooldowns, ht3, PLAYER_ID, NOW, QUEUE_CD)
        self.assertEqual(snap["queue_remaining"], 3 * 24 * 60 * 60 * 1000)
        self.assertEqual(snap["ht3"], {"randompot": "7d 0h 0m"})

    def test_ht3_cooldown_remaining(self):
        self.assertIsNone(eu.ht3_cooldown_remaining({}, PLAYER_ID, "randompot", NOW))
        ht3 = {PLAYER_ID: {"randompot": NOW + 1000}}
        self.assertEqual(eu.ht3_cooldown_remaining(ht3, PLAYER_ID, "randompot", NOW), 1000)
        self.assertIsNone(eu.ht3_cooldown_remaining(ht3, PLAYER_ID, "randompot", NOW + 5000))


class ApplyEditTests(unittest.TestCase):
    """apply_player_edit: transakce + audit, migrace, cooldowny, korupce."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _apply(self, edit, **kw):
        async def main():
            return await eu.apply_player_edit(
                player_id=PLAYER_ID,
                edit=edit,
                actor_id=7,
                actor_name="Admin",
                now=NOW,
                queue_cooldown_ms=QUEUE_CD,
                ht3_cooldown_ms=HT3_CD,
                **kw,
            )
        return asyncio.run(main())

    def _seed(self):
        _write("players.json", _players())

    # ------------------------------------------------------------------
    # IGN
    # ------------------------------------------------------------------
    def test_ign_edit_changes_db_and_audits(self):
        self._seed()
        result = self._apply({"field": "ign", "new_value": "AliceNew"})
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(result["old_value"], "AliceMC")
        self.assertEqual(result["new_value"], "AliceNew")
        self.assertEqual(result["field"], "ign")
        db = storage.load_data("players.json")
        self.assertEqual(db[0]["username"], "AliceNew")
        self.assertEqual(db[0]["discordId"], PLAYER_ID)
        # audit v téže transakci
        log = storage.load_data(eu.EDITUSER_LOG_FILE)
        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["actorId"], "7")
        self.assertEqual(entry["actorName"], "Admin")
        self.assertEqual(entry["playerId"], PLAYER_ID)
        self.assertEqual(entry["field"], "ign")
        self.assertEqual(entry["kit"], None)
        self.assertEqual(entry["oldValue"], "AliceMC")
        self.assertEqual(entry["newValue"], "AliceNew")
        self.assertEqual(entry["ts"], NOW)

    def test_ign_edit_conflict_is_error_and_safe(self):
        self._seed()
        result = self._apply({"field": "ign", "new_value": "BobMC"})
        self.assertEqual(result["status"], "error")
        self.assertIn("patří", result["message"])
        self.assertIsNone(result["audit"])
        self.assertEqual(storage.load_data("players.json"), _players())
        self.assertFalse(storage.load_data(eu.EDITUSER_LOG_FILE, None))

    def test_ign_edit_unchanged_is_idempotent(self):
        self._seed()
        first = self._apply({"field": "ign", "new_value": "AliceNew"})
        second = self._apply({"field": "ign", "new_value": "AliceNew"})
        self.assertEqual(first["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(second["status"], eu.OUTCOME_UNCHANGED)
        self.assertEqual(second["new_value"], "AliceNew")
        self.assertEqual(len(storage.load_data(eu.EDITUSER_LOG_FILE)), 1)

    def test_ign_change_migrates_eval_keys(self):
        self._seed()
        _write("evals.json", {"randompot": {"alicemc": NOW, "bobmc": NOW}})
        self._apply({"field": "ign", "new_value": "AliceNew"})
        evals = storage.load_data("evals.json")
        self.assertIn("alicenew", evals["randompot"])
        self.assertNotIn("alicemc", evals["randompot"])
        self.assertIn("bobmc", evals["randompot"])  # další hráč nedotčen

    # ------------------------------------------------------------------
    # Discord ID + migrace klíčů
    # ------------------------------------------------------------------
    def test_discord_edit_migrates_all_identity_keys(self):
        self._seed()
        _write("cooldowns.json", {PLAYER_ID: NOW})
        _write("ht3_cooldowns.json", {PLAYER_ID: {"randompot": NOW + HT3_CD}})
        _write("ht_tickets.json", {"chan1": {"ownerId": PLAYER_ID, "status": "open"}})
        _write("ht_results.json", {"res1": {"playerId": PLAYER_ID, "tier": "HT3"}})
        _write("queue.json", [{"id": PLAYER_ID, "kit": "randompot", "username": "AliceMC"}])
        _write("pulled_players.json", {PLAYER_ID: {"kit": "randompot"}})

        result = self._apply({"field": "discord_id", "new_value": NEW_ID})
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(result["old_value"], PLAYER_ID)
        self.assertEqual(
            set(result["changed_files"]),
            {"players.json", "cooldowns.json", "ht3_cooldowns.json",
             "ht_tickets.json", "ht_results.json", "queue.json",
             "pulled_players.json"},
        )

        players = storage.load_data("players.json")
        self.assertEqual(players[0]["discordId"], NEW_ID)
        self.assertEqual(players[0]["modes"], {"randompot": "HT2"})
        self.assertEqual(len(players), 2)  # žádné sloučení / nový hráč

        self.assertIn(NEW_ID, storage.load_data("cooldowns.json"))
        self.assertNotIn(PLAYER_ID, storage.load_data("cooldowns.json"))
        self.assertIn(NEW_ID, storage.load_data("ht3_cooldowns.json"))
        tickets = storage.load_data("ht_tickets.json")
        self.assertEqual(tickets["chan1"]["ownerId"], NEW_ID)
        results = storage.load_data("ht_results.json")
        self.assertEqual(results["res1"]["playerId"], NEW_ID)
        queue = storage.load_data("queue.json")
        self.assertEqual(queue[0]["id"], NEW_ID)
        pulled = storage.load_data("pulled_players.json")
        self.assertIn(NEW_ID, pulled)
        self.assertNotIn(PLAYER_ID, pulled)

        # audit
        entry = storage.load_data(eu.EDITUSER_LOG_FILE)[0]
        self.assertEqual(entry["playerId"], PLAYER_ID)  # dotaz byl na staré ID
        self.assertEqual(entry["oldValue"], PLAYER_ID)
        self.assertEqual(entry["newValue"], NEW_ID)

    def test_discord_edit_conflict_is_error_and_safe(self):
        self._seed()
        result = self._apply({"field": "discord_id", "new_value": OTHER_ID})
        self.assertEqual(result["status"], "error")
        self.assertIn("konflikt", result["message"])
        self.assertEqual(storage.load_data("players.json"), _players())
        self.assertFalse(storage.load_data(eu.EDITUSER_LOG_FILE, None))

    # ------------------------------------------------------------------
    # Tier
    # ------------------------------------------------------------------
    def test_tier_edit_changes_db_audits_keeps_history(self):
        self._seed()
        result = self._apply(
            {"field": "tier", "kit": "randompot", "tier": "HT3", "retired": False}
        )
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(result["kit"], "randompot")
        self.assertEqual(result["old_value"], "HT2")
        self.assertEqual(result["new_value"], "HT3")
        db = storage.load_data("players.json")
        self.assertEqual(db[0]["modes"], {"randompot": "HT3"})
        self.assertIn("randompot", db[0]["history"])  # historie zůstává
        entry = storage.load_data(eu.EDITUSER_LOG_FILE)[0]
        self.assertEqual(entry["field"], "tier")
        self.assertEqual(entry["kit"], "randompot")
        self.assertEqual(entry["oldValue"], "HT2")
        self.assertEqual(entry["newValue"], "HT3")

    def test_tier_invalid_transition_is_error(self):
        self._seed()
        result = self._apply(
            {"field": "tier", "kit": "randompot", "tier": "RHT3", "retired": True}
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(storage.load_data("players.json"), _players())

    def test_tier_edit_unchanged(self):
        self._seed()
        result = self._apply(
            {"field": "tier", "kit": "randompot", "tier": "HT2", "retired": False}
        )
        self.assertEqual(result["status"], eu.OUTCOME_UNCHANGED)
        self.assertFalse(storage.load_data(eu.EDITUSER_LOG_FILE, None))

    # ------------------------------------------------------------------
    # Cooldowny
    # ------------------------------------------------------------------
    def test_cooldown_clear_queue(self):
        _write("cooldowns.json", {PLAYER_ID: NOW})
        result = self._apply({"field": "cooldown", "action": "clear_queue"})
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertNotIn(PLAYER_ID, storage.load_data("cooldowns.json"))
        entry = storage.load_data(eu.EDITUSER_LOG_FILE)[0]
        self.assertEqual(entry["field"], "cooldown")
        self.assertEqual(entry["kit"], None)
        self.assertEqual(entry["oldValue"], "waitlist: 4d 0h 0m")
        self.assertEqual(entry["newValue"], "waitlist: žádný")

    def test_cooldown_set_queue(self):
        result = self._apply({"field": "cooldown", "action": "set_queue"})
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(storage.load_data("cooldowns.json"), {PLAYER_ID: NOW})

    def test_cooldown_set_ht3_requires_kit(self):
        result = self._apply({"field": "cooldown", "action": "set_ht3"})
        self.assertEqual(result["status"], "error")
        self.assertIn("kit", result["message"])

    def test_cooldown_clear_ht3(self):
        _write("ht3_cooldowns.json", {PLAYER_ID: {"randompot": NOW + HT3_CD}})
        result = self._apply(
            {"field": "cooldown", "action": "clear_ht3", "kit": "randompot"}
        )
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(storage.load_data("ht3_cooldowns.json"), {PLAYER_ID: {}})
        entry = storage.load_data(eu.EDITUSER_LOG_FILE)[0]
        self.assertEqual(entry["kit"], "randompot")
        self.assertEqual(entry["newValue"], "HT3 randompot: žádný")

    def test_cooldown_set_ht3(self):
        result = self._apply(
            {"field": "cooldown", "action": "set_ht3", "kit": "randompot"}
        )
        self.assertEqual(result["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(
            storage.load_data("ht3_cooldowns.json"),
            {PLAYER_ID: {"randompot": NOW + HT3_CD}},
        )

    def test_cooldown_edit_unchanged_reports_idempotent(self):
        _write("ht3_cooldowns.json", {PLAYER_ID: {"randompot": NOW + HT3_CD}})
        first = self._apply(
            {"field": "cooldown", "action": "clear_ht3", "kit": "randompot"}
        )
        second = self._apply(
            {"field": "cooldown", "action": "clear_ht3", "kit": "randompot"}
        )
        self.assertEqual(first["status"], eu.OUTCOME_CHANGED)
        self.assertEqual(second["status"], eu.OUTCOME_UNCHANGED)
        self.assertEqual(len(storage.load_data(eu.EDITUSER_LOG_FILE)), 1)

    # ------------------------------------------------------------------
    # Poškozená data (DataCorruptionError → bezpečný abort, žádný overwrite)
    # ------------------------------------------------------------------
    def test_corrupt_players_json_safe_abort(self):
        self._seed()
        with open(storage.data_path("players.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        result = self._apply(
            {"field": "tier", "kit": "randompot", "tier": "HT3", "retired": False}
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("Poškozená data", result["message"])
        self.assertIsNone(result["audit"])
        with open(storage.data_path("players.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")  # nikdy se nepřepíše

    def test_corrupt_audit_log_safe_abort(self):
        self._seed()
        with open(storage.data_path(eu.EDITUSER_LOG_FILE), "w", encoding="utf-8") as f:
            f.write("{not json")
        result = self._apply({"field": "ign", "new_value": "AliceNew"})
        self.assertEqual(result["status"], "error")
        self.assertIn("Poškozená data", result["message"])
        self.assertEqual(storage.load_data("players.json"), _players())
        with open(storage.data_path(eu.EDITUSER_LOG_FILE), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_unknown_edit_field_is_error(self):
        self._seed()
        result = self._apply({"field": "cooldown", "action": "nonsense"})
        self.assertEqual(result["status"], "error")


class ExecuteEditTests(unittest.TestCase):
    """execute_player_edit: DB → Discord role → web, reporty a best effort."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        _write("players.json", _players())

    def _run(self, edit, *, role_context=None, apply_roles=None, push_web=None):
        async def main():
            return await eu.execute_player_edit(
                player_id=PLAYER_ID,
                edit=edit,
                actor_id=7,
                actor_name="Admin",
                now=NOW,
                queue_cooldown_ms=QUEUE_CD,
                ht3_cooldown_ms=HT3_CD,
                role_context=role_context,
                apply_roles=apply_roles,
                push_web=push_web,
            )
        return asyncio.run(main())

    def test_ign_edit_full_success(self):
        push_web = mock.AsyncMock(return_value={"ok": True, "message": "OK"})
        report = self._run(
            {"field": "ign", "new_value": "AliceNew"}, push_web=push_web
        )
        self.assertEqual(report["status"], eu.STATUS_SUCCESS)
        self.assertEqual(report["db"]["status"], eu.OUTCOME_CHANGED)
        self.assertTrue(report["roles"]["skipped"])  # role jen pro tier úpravy
        self.assertFalse(report["web"]["skipped"])
        self.assertTrue(report["web"]["ok"])
        push_web.assert_awaited_once()
        canonical = push_web.await_args.args[0]
        self.assertEqual(canonical[0]["username"], "AliceNew")

    def test_web_failure_is_partial_and_db_safe(self):
        push_web = mock.AsyncMock(return_value={"ok": False, "message": "GitHub 500"})
        report = self._run(
            {"field": "ign", "new_value": "AliceNew"}, push_web=push_web
        )
        self.assertEqual(report["status"], eu.STATUS_PARTIAL)
        self.assertFalse(report["web"]["ok"])
        self.assertIn("GitHub 500", report["web"]["errors"])
        # kanonická změna se nikdy neztratí kvůli webu
        db = storage.load_data("players.json")
        self.assertEqual(db[0]["username"], "AliceNew")
        self.assertEqual(len(storage.load_data(eu.EDITUSER_LOG_FILE)), 1)

    def test_web_exception_is_partial(self):
        async def boom(_canonical):
            raise RuntimeError("network down")

        report = self._run({"field": "ign", "new_value": "AliceNew"}, push_web=boom)
        self.assertEqual(report["status"], eu.STATUS_PARTIAL)
        self.assertFalse(report["web"]["ok"])
        self.assertTrue(any("network down" in e for e in report["web"]["errors"]))
        self.assertEqual(storage.load_data("players.json")[0]["username"], "AliceNew")

    def test_web_skipped_without_push_web(self):
        report = self._run({"field": "ign", "new_value": "AliceNew"})
        self.assertEqual(report["status"], eu.STATUS_SUCCESS)
        self.assertTrue(report["web"]["skipped"])

    def test_tier_edit_applies_role_actions(self):
        role_context = {
            "member": make_member(PLAYER_ID, "AliceMC", {"102"}),
            "roles_map": _kit_roles_map(),
            "kit_display": {"randompot": "RandomPot"},
        }
        applied = []

        async def apply_roles(actions):
            applied.append(actions)
            return [
                {"op": a["op"], "roleId": a["role_id"], "ok": True, "error": None}
                for a in actions
            ]

        push_web = mock.AsyncMock(return_value={"ok": True, "message": "OK"})
        report = self._run(
            {"field": "tier", "kit": "randompot", "tier": "HT3", "retired": False},
            role_context=role_context,
            apply_roles=apply_roles,
            push_web=push_web,
        )
        self.assertEqual(report["status"], eu.STATUS_SUCCESS)
        self.assertEqual(len(applied), 1)  # apply_roles se volalo jednou
        input_ops = {(a["op"], a["role_id"]) for a in applied[0]}
        self.assertEqual(input_ops, {("add", "101"), ("remove", "102")})
        report_ops = {(a["op"], a["roleId"]) for a in report["roles"]["actions"]}
        self.assertEqual(report_ops, {("add", "101"), ("remove", "102")})
        self.assertTrue(report["web"]["ok"])

    def test_discord_api_failure_is_partial_but_db_changed(self):
        role_context = {
            "member": make_member(PLAYER_ID, "AliceMC", set()),
            "roles_map": _kit_roles_map(),
            "kit_display": {"randompot": "RandomPot"},
        }

        async def apply_roles(_actions):
            return [{"op": "add", "roleId": "101", "ok": False, "error": "Forbidden"}]

        report = self._run(
            {"field": "tier", "kit": "randompot", "tier": "HT3", "retired": False},
            role_context=role_context,
            apply_roles=apply_roles,
        )
        self.assertEqual(report["status"], eu.STATUS_PARTIAL)
        self.assertIn("Forbidden", report["roles"]["errors"])
        self.assertEqual(storage.load_data("players.json")[0]["modes"]["randompot"], "HT3")
        self.assertEqual(len(storage.load_data(eu.EDITUSER_LOG_FILE)), 1)

    def test_role_apply_exception_is_partial(self):
        role_context = {
            "member": make_member(PLAYER_ID, "AliceMC", set()),
            "roles_map": _kit_roles_map(),
            "kit_display": {"randompot": "RandomPot"},
        }

        async def apply_roles(_actions):
            raise RuntimeError("Discord API down")

        report = self._run(
            {"field": "tier", "kit": "randompot", "tier": "HT3", "retired": False},
            role_context=role_context,
            apply_roles=apply_roles,
        )
        self.assertEqual(report["status"], eu.STATUS_PARTIAL)
        self.assertTrue(
            any("Discord API down" in e for e in report["roles"]["errors"])
        )
        self.assertEqual(storage.load_data("players.json")[0]["modes"]["randompot"], "HT3")

    def test_tier_edit_skips_roles_when_member_missing(self):
        role_context = {
            "member": None,  # člen není na serveru
            "roles_map": _kit_roles_map(),
            "kit_display": {"randompot": "RandomPot"},
        }
        called = []

        async def apply_roles(_actions):
            called.append(True)
            return []

        report = self._run(
            {"field": "tier", "kit": "randompot", "tier": "HT3", "retired": False},
            role_context=role_context,
            apply_roles=apply_roles,
        )
        self.assertEqual(report["status"], eu.STATUS_SUCCESS)
        self.assertTrue(report["roles"]["skipped"])
        self.assertIn("není na serveru", report["roles"]["note"])
        self.assertEqual(called, [])  # žádné akce se nevolaly

    def test_idempotent_repeat_skips_roles_and_web(self):
        push_web = mock.AsyncMock(return_value={"ok": True, "message": "OK"})
        first = self._run({"field": "ign", "new_value": "AliceNew"}, push_web=push_web)
        second = self._run({"field": "ign", "new_value": "AliceNew"}, push_web=push_web)
        self.assertEqual(first["status"], eu.STATUS_SUCCESS)
        self.assertEqual(second["status"], eu.STATUS_SUCCESS)
        self.assertEqual(second["db"]["status"], eu.OUTCOME_UNCHANGED)
        self.assertIn("idempotentní", second["message"])
        self.assertTrue(second["roles"]["skipped"])
        self.assertTrue(second["web"]["skipped"])
        self.assertEqual(push_web.await_count, 1)
        self.assertEqual(len(storage.load_data(eu.EDITUSER_LOG_FILE)), 1)

    def test_db_failure_is_failure_status(self):
        with open(storage.data_path("players.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        push_web = mock.AsyncMock()
        report = self._run({"field": "ign", "new_value": "AliceNew"}, push_web=push_web)
        self.assertEqual(report["status"], eu.STATUS_FAILURE)
        self.assertEqual(report["db"]["status"], "error")
        self.assertIsNone(report["roles"])  # selhání DB → bez role/web synchronizace
        self.assertIsNone(report["web"])
        push_web.assert_not_awaited()

    def test_edituser_log_accessible(self):
        async def main():
            await eu.apply_player_edit(
                player_id=PLAYER_ID,
                edit={"field": "ign", "new_value": "AliceNew"},
                actor_id=7, actor_name="Admin", now=NOW,
                queue_cooldown_ms=QUEUE_CD, ht3_cooldown_ms=HT3_CD,
            )
            entries = await eu.get_edituser_log()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["field"], "ign")
        asyncio.run(main())


class CogPermissionTests(unittest.TestCase):
    """/edituser a jeho view – POUZE admini (has_admin_role / ADMIN_ROLE_IDS)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

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
            player = SimpleNamespace(id=PLAYER_ID)

            async def main():
                await EditUser.edituser.callback(cog, inter, player)

            asyncio.run(main())
            inter.response.defer.assert_awaited_once()  # prošlo admin gate
            inter.followup.send.assert_awaited_once()  # hráč nenalezen (prázdná DB)
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
    """Stale check mezi náhledem a potvrzením (ConfirmEditView._stale_check)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        _write("players.json", _players())

    def _view(self, stale):
        return ConfirmEditView(cog=None, payload={"player_id": PLAYER_ID, "stale": stale})

    def test_stale_ok_when_unchanged(self):
        self.assertIsNone(self._check({"field": "tier", "kit": "randompot", "old_value": "HT2"}))
        self.assertIsNone(self._check({"field": "ign", "old_value": "AliceMC"}))
        self.assertIsNone(self._check({"field": "discord_id", "old_value": PLAYER_ID}))

    def _check(self, stale):
        async def main():
            return await self._view(stale)._stale_check()
        return asyncio.run(main())

    def test_tier_changed_flagged_stale(self):
        players = storage.load_data("players.json")
        players[0]["modes"] = {"randompot": "HT3"}
        _write("players.json", players)
        msg = self._check({"field": "tier", "kit": "randompot", "old_value": "HT2"})
        self.assertIn("změnil", msg)
        self.assertIn("HT3", msg)

    def test_ign_changed_flagged_stale(self):
        players = storage.load_data("players.json")
        players[0]["username"] = "AliceNew"
        _write("players.json", players)
        msg = self._check({"field": "ign", "old_value": "AliceMC"})
        self.assertIn("změnilo", msg)

    def test_discord_changed_flagged_stale(self):
        # Po změně Discord ID už staré ID nelze dohledat → stale → nic se
        # neaplikuje (bezpečný abort i v situaci, kdy hráč existuje pod jiným ID).
        players = storage.load_data("players.json")
        players[0]["discordId"] = NEW_ID
        _write("players.json", players)
        msg = self._check({"field": "discord_id", "old_value": PLAYER_ID})
        self.assertIn("není", msg)

    def test_missing_player_flagged_stale(self):
        _write("players.json", [])
        msg = self._check({"field": "ign", "old_value": "AliceMC"})
        self.assertIn("není", msg)

    def test_display_case_mode_key_matches_stale(self):
        """Stale check tieru musí najít display-case klíč ("RandomPot")."""
        players = storage.load_data("players.json")
        players[0]["modes"] = {"RandomPot": "HT2"}
        _write("players.json", players)
        msg = self._check({"field": "tier", "kit": "randompot", "old_value": "HT2"})
        self.assertIsNone(msg)


if __name__ == "__main__":
    unittest.main()
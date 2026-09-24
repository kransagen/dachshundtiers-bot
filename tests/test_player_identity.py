"""Testy identity hráče – services/player_identity.py (bez discord.py).

Discord ID je primární identita hráče; IGN je jen mutovatelný údaj. Legacy
záznamy players.json BEZ ``discordId`` se při shodě IGN ADOPTUJÍ (připojí se
Discord ID, historie a tiery zůstávají) – adopce NENÍ spojení dvou hráčů.
IGN patřící záznamu s JINÝM Discord ID = konflikt (PlayerIdentityConflict) –
operace se odmítá, nikdy se nehádá a nikdy neslučuje automaticky.
"""

import copy
import unittest

from services.player_identity import (
    CLAIM_ADOPTED,
    CLAIM_CREATED,
    CLAIM_RENAMED,
    CLAIM_UNCHANGED,
    RESOLVE_DISCORD_ID,
    RESOLVE_IGN,
    PlayerIdentityConflict,
    claim_ign,
    find_by_discord_id,
    find_by_ign,
    resolve_player,
)


def _player(username, *, discord_id=None, modes=None, history=None):
    p = {
        "username": username,
        "modes": dict(modes or {}),
        "history": {k: list(v) for k, v in (history or {}).items()},
    }
    if discord_id is not None:
        p["discordId"] = discord_id
    return p


class FindTests(unittest.TestCase):
    def test_find_by_discord_id(self):
        players = [_player("A", discord_id="1"), _player("B")]
        self.assertEqual(find_by_discord_id(players, "1")["username"], "A")
        self.assertIsNone(find_by_discord_id(players, "999"))
        self.assertIsNone(find_by_discord_id(players, None))
        self.assertIsNone(find_by_discord_id([], "1"))

    def test_find_by_ign_is_case_insensitive(self):
        players = [_player("AliceMC")]
        self.assertEqual(find_by_ign(players, "aliceMC")["username"], "AliceMC")
        self.assertEqual(find_by_ign(players, "ALICEMC")["username"], "AliceMC")
        self.assertIsNone(find_by_ign(players, "bob"))
        self.assertIsNone(find_by_ign(players, ""))

    def test_find_skips_non_dict_entries(self):
        players = [_player("A", discord_id="1"), "junk", None]
        self.assertEqual(find_by_discord_id(players, "1")["username"], "A")
        self.assertEqual(find_by_ign(players, "A")["username"], "A")

    def test_resolve_player_prefers_discord_id(self):
        players = [_player("AliceMC", discord_id="1")]
        player, source = resolve_player(players, discord_id="1", ign="AliceMC")
        self.assertEqual(source, RESOLVE_DISCORD_ID)
        self.assertEqual(player["username"], "AliceMC")
        player, source = resolve_player(players, ign="aliceMC")
        self.assertEqual(source, RESOLVE_IGN)
        self.assertIsNone(resolve_player(players, ign="nobody")[0])


class ClaimTests(unittest.TestCase):
    def test_created_when_nothing_matches(self):
        players = []
        players, player, outcome = claim_ign(players, discord_id="1", ign="Newbie")
        self.assertEqual(outcome, CLAIM_CREATED)
        self.assertEqual(player["username"], "Newbie")
        self.assertEqual(player["discordId"], "1")
        self.assertEqual(len(players), 1)

    def test_unchanged_when_discord_matches_same_ign(self):
        players = [_player("AliceMC", discord_id="1")]
        players, player, outcome = claim_ign(players, discord_id="1", ign="AliceMC")
        self.assertEqual(outcome, CLAIM_UNCHANGED)
        self.assertEqual(len(players), 1)
        self.assertEqual(player["discordId"], "1")

    def test_renamed_when_discord_matches_new_ign(self):
        players = [_player("AliceMC", discord_id="1")]
        players, player, outcome = claim_ign(players, discord_id="1", ign="NewName")
        self.assertEqual(outcome, CLAIM_RENAMED)
        self.assertEqual(player["username"], "NewName")
        self.assertEqual(player["discordId"], "1")
        self.assertEqual(len(players), 1)

    def test_adopts_legacy_record_without_discord_id(self):
        legacy = _player(
            "mendu__", modes={"MolePVP": "LT3"},
            history={"MolePVP": [{"date": "1.1.2026", "tier": "LT3"}]},
        )
        players = [legacy]
        players, player, outcome = claim_ign(players, discord_id="1", ign="mendu__")
        self.assertEqual(outcome, CLAIM_ADOPTED)
        self.assertEqual(player["discordId"], "1")
        self.assertEqual(player["modes"]["MolePVP"], "LT3")
        self.assertEqual(len(player["history"]["MolePVP"]), 1)
        self.assertEqual(len(players), 1)

    def test_conflict_when_ign_belongs_to_other_discord(self):
        players = [_player("mendu__", discord_id="999")]
        with self.assertRaises(PlayerIdentityConflict):
            claim_ign(players, discord_id="1", ign="mendu__")

    def test_conflict_when_rename_target_ign_taken(self):
        players = [_player("AliceMC", discord_id="1"), _player("bob")]
        with self.assertRaises(PlayerIdentityConflict):
            claim_ign(players, discord_id="1", ign="Bob")

    def test_legacy_no_discord_path(self):
        players = [_player("AliceMC")]
        _, player, outcome = claim_ign(players, discord_id=None, ign="AliceMC")
        self.assertEqual(outcome, CLAIM_UNCHANGED)
        _, player, outcome = claim_ign(players, discord_id="", ign="AliceMC")
        self.assertEqual(outcome, CLAIM_UNCHANGED)
        players, player, outcome = claim_ign(players, discord_id=None, ign="novy")
        self.assertEqual(outcome, CLAIM_CREATED)
        self.assertEqual(player["username"], "novy")

    def test_input_never_mutated(self):
        players = [_player("AliceMC", discord_id="1")]
        before = copy.deepcopy(players)
        claim_ign(players, discord_id="1", ign="NewName")
        self.assertEqual(players, before)

    def test_empty_ign_rejected(self):
        with self.assertRaises(PlayerIdentityConflict):
            claim_ign([], discord_id="1", ign="   ")


if __name__ == "__main__":
    unittest.main()
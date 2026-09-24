"""Testy centrálního RoleSyncService – services/role_sync.py (bez discord.py).

Ověřují nové schopnosti z auditu:
- retired tier v players.json (R-prefix) se reportuje a NIKDY neovlivní role,
- retired role z kit_roles.json nezpůsobí wrong_role / unknown_player,
- hráč s discordId se páruje podle ID (permanentní identita) i při změně IGN,
- fasáda analyze_role_sync má stejné chování jako analyze_sync se zapnutým
  retired + discordId párováním.
"""

import unittest

from services import role_sync
from services.playersync import analyze_sync, make_member

ROLES = {"anchorpvp": {"HT3": "102", "LT3": "104"}}
DISPLAY = {"anchorpvp": "AnchorPvP"}


def _member(member_id, name, roles, extra=None):
    return make_member(member_id, name, roles, extra_names=extra)


class RetiredTierTests(unittest.TestCase):
    def test_retired_tier_reported_not_synced(self):
        players = [{"username": "RetiredMC", "modes": {"AnchorPvP": "RLT2"}}]
        members = [_member("1", "RetiredMC", set())]
        analysis = role_sync.analyze_role_sync(players, members, ROLES, DISPLAY)
        self.assertEqual(analysis["summary"]["retired_tier_in_db"], 1)
        self.assertEqual(analysis["summary"]["invalid_tier"], 0)
        self.assertEqual(analysis["summary"]["missing_role"], 0)
        self.assertFalse(analysis["has_actions"])  # nic se nenavrhuje
        f = analysis["findings"][0]
        self.assertEqual(f["kind"], "retired_tier_in_db")
        self.assertIsNone(f["action"])

    def test_retired_role_ignored(self):
        roles = {"anchorpvp": {"HT3": "102", "RLT2": "105"}}
        members = [_member("1", "AliceMC", {"105"})]
        analysis = role_sync.analyze_role_sync([], members, roles, DISPLAY)
        self.assertEqual(analysis["summary"]["wrong_role"], 0)
        self.assertEqual(analysis["summary"]["unknown_player"], 0)
        self.assertEqual(analysis["summary"]["multiple_roles"], 0)
        self.assertFalse(analysis["findings"])
        self.assertFalse(analysis["has_actions"])

    def test_retired_tier_plus_current_role_is_wrong_role(self):
        players = [{"username": "RetiredMC", "modes": {"AnchorPvP": "RLT2"}}]
        members = [_member("1", "RetiredMC", {"102"})]  # drží aktuální HT3
        analysis = role_sync.analyze_role_sync(players, members, ROLES, DISPLAY)
        self.assertEqual(analysis["summary"]["retired_tier_in_db"], 1)
        self.assertEqual(analysis["summary"]["wrong_role"], 1)  # HT3 se odebírá
        self.assertEqual([a["role_id"] for a in analysis["actions"]], ["102"])

    def test_explicit_retired_list(self):
        players = [{"username": "X", "modes": {"AnchorPvP": "LGD"}}]  # ne R-prefix
        analysis = role_sync.analyze_role_sync(
            players, [_member("1", "X", set())], ROLES, DISPLAY,
            retired_tiers=["LGD"],
        )
        self.assertEqual(analysis["summary"]["retired_tier_in_db"], 1)
        self.assertEqual(analysis["summary"]["invalid_tier"], 0)


class DiscordIdMatchingTests(unittest.TestCase):
    def test_discord_id_wins_over_name(self):
        # IGN se změnilo, ale discordId (permanentní identita) sedí
        players = [
            {"username": "OldIGN", "discordId": "1", "modes": {"AnchorPvP": "HT3"}}
        ]
        members = [_member("1", "NewIGN", {"909"})]
        analysis = role_sync.analyze_role_sync(players, members, ROLES, DISPLAY)
        self.assertEqual(analysis["summary"]["missing_role"], 1)
        self.assertEqual(analysis["summary"]["missing_player"], 0)
        f = analysis["findings"][0]
        self.assertEqual(f["member_id"], "1")
        self.assertEqual(f["ign"], "OldIGN")
        self.assertEqual(f["action"]["op"], "add")
        self.assertEqual(f["action"]["role_id"], "102")

    def test_discord_id_without_member_is_missing_player(self):
        players = [
            {"username": "Gone", "discordId": "999", "modes": {"AnchorPvP": "HT3"}}
        ]
        analysis = role_sync.analyze_role_sync(players, [], ROLES, DISPLAY)
        self.assertEqual(analysis["summary"]["missing_player"], 1)

    def test_discord_id_in_wrong_role_lookup(self):
        # hráč s discordId drží roli jiného tieru; jménem by spadl na Fake
        players = [
            {"username": "Real", "discordId": "1", "modes": {"OtherKit": "LT3"}},
            {"username": "Fake", "modes": {}},
        ]
        members = [_member("1", "Fake", {"102"})]
        analysis = role_sync.analyze_role_sync(players, members, ROLES, DISPLAY)
        self.assertEqual(analysis["summary"]["wrong_role"], 1)
        f = analysis["findings"][0]
        self.assertEqual(f["ign"], "Real")  # identita podle discordId

    def test_facade_matches_underlying_engine(self):
        players = [{"username": "AliceMC", "modes": {"AnchorPvP": "HT3"}}]
        members = [_member("1", "AliceMC", {"909"})]
        direct = analyze_sync(
            players, members, ROLES, DISPLAY,
            retired_tiers=None, match_by_discord_id=True,
        )
        via_facade = role_sync.analyze_role_sync(players, members, ROLES, DISPLAY)
        self.assertEqual(direct["summary"], via_facade["summary"])
        self.assertEqual(direct["findings"], via_facade["findings"])
        self.assertEqual(direct["actions"], via_facade["actions"])


if __name__ == "__main__":
    unittest.main()
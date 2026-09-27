"""Dual-mode testy views.py (Phase F #10c).

Ověřují, že interaktivní komponenty (JoinModal, QueueView, HT3PanelView,
HT3Modal, PullChannelSelectView) předávají ``session_factory`` do služeb
a že JSON pre-checky (closed fronta / cooldown / duplicita) běží přes
duální služby (queue_state / get_cooldowns / list_queue_entries) místo
přímého čtení JSON souborů.

Diskutovaný MagicMock trap: u ``mock.MagicMock()`` interakce je
``interaction.client`` auto-MagicMock (truthy) => ``_session_factory`` by
tiše vrátil falešný session_factory a testy by šly po DB cestě. Všechny
testy proto používají ``interaction.client = None`` (JSON režim) nebo
explicitní fake session_factory (DB režim).
"""

import unittest
from unittest import mock

from views import (
    HT3Modal,
    HT3PanelView,
    JoinModal,
    QueueView,
    _session_factory,
)


def _interaction(db_factory=None):
    """Interakce s deterministickým režimem (None = JSON, jinak DB)."""
    inter = mock.MagicMock()
    inter.channel_id = 1001
    inter.user.id = "99"
    inter.user.display_name = "tester-one"
    inter.client = mock.MagicMock()
    inter.client.db_session_factory = db_factory
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.response.send_modal = mock.AsyncMock()
    return inter


class SessionFactoryTests(unittest.TestCase):
    def test_json_mode_returns_none(self):
        inter = _interaction(db_factory=None)
        self.assertIsNone(_session_factory(inter))

    def test_db_mode_returns_factory(self):
        factory = object()
        inter = _interaction(db_factory=factory)
        self.assertIs(_session_factory(inter), factory)

    def test_missing_client_returns_none(self):
        inter = mock.MagicMock()
        del inter.client
        self.assertIsNone(_session_factory(inter))


class JoinModalTests(unittest.TestCase):
    def setUp(self):
        self.modal = JoinModal("AnchorPvP")
        self.modal.ign_input = mock.MagicMock(value="AliceMC")

    def _submit(self, status):
        import asyncio
        inter = _interaction()
        with mock.patch("views.join_queue", new=mock.AsyncMock(
            return_value={"result": status, "remaining": 0}
        )) as join_queue, \
             mock.patch("views.update_panel", new=mock.AsyncMock()) as update_panel:
            asyncio.run(self.modal.on_submit(inter))
        return join_queue, update_panel, inter

    def test_passes_session_factory_to_join_queue(self):
        join_queue, _, _ = self._submit("joined")
        self.assertEqual(join_queue.call_args.kwargs["session_factory"], None)
        self.assertEqual(join_queue.call_args.args[3], "AnchorPvP")

    def test_joined_refreshes_panel_with_sf(self):
        _, update_panel, _ = self._submit("joined")
        self.assertEqual(update_panel.call_args.kwargs["session_factory"], None)

    def test_closed_queue_message(self):
        _, _, inter = self._submit("closed")
        inter.response.send_message.assert_called_once()
        self.assertIn("zavřena", inter.response.send_message.call_args.args[0])

    def test_duplicate_does_not_refresh_panel(self):
        _, update_panel, _ = self._submit("duplicate")
        update_panel.assert_not_called()


class QueueViewTests(unittest.TestCase):
    def setUp(self):
        self.view = QueueView("AnchorPvP")

    def _run_join(self, inter=None, **patches):
        import asyncio
        inter = inter or _interaction()
        defaults = {
            "queue_state": mock.AsyncMock(return_value={"name": "AnchorPvP"}),
            "get_cooldowns": mock.AsyncMock(return_value={"waitlist_ms": None, "ht3": {}}),
            "list_queue_entries": mock.AsyncMock(return_value=[]),
        }
        defaults.update(patches)
        with mock.patch("views.queue_state", new=defaults["queue_state"]), \
             mock.patch("views.get_cooldowns", new=defaults["get_cooldowns"]), \
             mock.patch("views.list_queue_entries", new=defaults["list_queue_entries"]), \
             mock.patch("views.JoinModal") as join_modal:
            asyncio.run(self.view.on_join(inter))
        return inter, defaults, join_modal

    def test_join_closed_queue(self):
        inter, _, _ = self._run_join(queue_state=mock.AsyncMock(return_value=None))
        inter.response.send_message.assert_called_once()
        self.assertIn("zavřena", inter.response.send_message.call_args.args[0])

    def test_join_cooldown(self):
        inter, _, _ = self._run_join(
            get_cooldowns=mock.AsyncMock(
                return_value={"waitlist_ms": 1234, "ht3": {}}
            )
        )
        inter.response.send_message.assert_called_once()
        self.assertIn("cooldown", inter.response.send_message.call_args.args[0])

    def test_join_duplicate_in_queue(self):
        inter, _, _ = self._run_join(
            list_queue_entries=mock.AsyncMock(
                return_value=[{"id": "99", "kit": "anchorpvp"}]
            )
        )
        inter.response.send_message.assert_called_once()
        self.assertIn("zapsaný", inter.response.send_message.call_args.args[0])

    def test_join_opens_modal(self):
        inter, _, join_modal = self._run_join()
        join_modal.assert_called_once_with("AnchorPvP")
        inter.response.send_modal.assert_called_once()

    def test_join_passes_sf_to_services(self):
        factory = object()
        inter = _interaction(db_factory=factory)
        _, patches, _ = self._run_join(inter=inter)
        self.assertEqual(
            patches["queue_state"].call_args.kwargs["session_factory"], factory
        )
        self.assertEqual(
            patches["get_cooldowns"].call_args.kwargs["session_factory"], factory
        )
        self.assertEqual(
            patches["list_queue_entries"].call_args.kwargs["session_factory"], factory
        )

    def test_leave_passes_sf(self):
        import asyncio
        inter = _interaction()
        with mock.patch("views.leave_queue", new=mock.AsyncMock(return_value=True)) as lq, \
             mock.patch("views.update_panel", new=mock.AsyncMock()) as up:
            asyncio.run(self.view.on_leave(inter))
        self.assertEqual(lq.call_args.kwargs["session_factory"], None)
        self.assertEqual(up.call_args.kwargs["session_factory"], None)

    def test_pull_empty_queue(self):
        import asyncio
        inter = _interaction()
        inter.user = mock.MagicMock()
        with mock.patch("views.has_tester_role", return_value=True), \
             mock.patch("views.list_queue_entries", new=mock.AsyncMock(return_value=[])):
            asyncio.run(self.view.on_pull(inter))
        inter.response.send_message.assert_called_once()
        self.assertIn("prázdná", inter.response.send_message.call_args.args[0])


class HT3PanelViewTests(unittest.TestCase):
    def test_default_kits_from_utils(self):
        view = HT3PanelView()
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].custom_id, "ht3_select_kit")

    def test_db_kits_override_defaults(self):
        view = HT3PanelView(kits=["AnchorPvP", "BedWars"])
        options = [opt.label for opt in view.children[0].options]
        self.assertEqual(options, ["AnchorPvP", "BedWars"])

    def test_on_select_cooldown_blocks(self):
        import asyncio
        inter = _interaction()
        inter.data = {"values": ["AnchorPvP"]}
        view = HT3PanelView(kits=["AnchorPvP"])
        with mock.patch("views.get_cooldowns", new=mock.AsyncMock(
            return_value={"waitlist_ms": None, "ht3": {"AnchorPvP": 1000}}
        )):
            asyncio.run(view.on_select(inter))
        inter.response.send_message.assert_called_once()
        self.assertIn("cooldown", inter.response.send_message.call_args.args[0])

    def test_on_select_no_cooldown_opens_modal(self):
        import asyncio
        inter = _interaction()
        inter.data = {"values": ["AnchorPvP"]}
        view = HT3PanelView(kits=["AnchorPvP"])
        with mock.patch("views.get_cooldowns", new=mock.AsyncMock(
            return_value={"waitlist_ms": None, "ht3": {}}
        )), mock.patch("views.HT3Modal") as modal:
            asyncio.run(view.on_select(inter))
        modal.assert_called_once_with("AnchorPvP")
        inter.response.send_modal.assert_called_once()


class HT3ModalTests(unittest.TestCase):
    """HT3Modal.on_submit – SF propagace do služeb (player_tier, has_eval,
    find_open_ticket, create_ticket, set_panel_message, log_ticket_event)."""

    def setUp(self):
        self.modal = HT3Modal("AnchorPvP")
        self.modal.ign_input = mock.MagicMock(value="AliceMC")
        self.modal.tier_input = mock.MagicMock(value="HT3")

    def _channel(self):
        channel = mock.MagicMock()
        channel.id = 2001
        channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=3001))
        channel.delete = mock.AsyncMock()
        return channel

    def _guild(self):
        guild = mock.MagicMock()
        guild.get_member.return_value = mock.MagicMock()
        guild.get_channel.return_value = mock.MagicMock(id=4001)
        guild.create_text_channel = mock.AsyncMock(return_value=self._channel())
        return guild

    def _ticket(self):
        return {
            "id": "2001", "status": "open", "ownerId": "99", "ownerName": "alice",
            "ign": "AliceMC", "kit": "AnchorPvP", "targetTier": "HT3",
            "currentTier": "LT3", "eval": True, "claimerId": None, "members": [],
        }

    def test_sf_propagated_to_all_services(self):
        import asyncio
        factory = object()
        inter = _interaction(db_factory=factory)
        inter.guild = self._guild()
        inter.followup.send = mock.AsyncMock()

        with mock.patch("views.player_tier", new=mock.AsyncMock(return_value="LT3")) as pt, \
             mock.patch("views.has_eval", new=mock.AsyncMock(return_value=True)) as he, \
             mock.patch("views.tier_allows_tickets", return_value=True), \
             mock.patch("views.effective_ticket_tier", return_value="LT3E"), \
             mock.patch("views.next_ticket_tier", return_value="HT3"), \
             mock.patch("views.find_open_ticket", new=mock.AsyncMock(return_value=None)) as fot, \
             mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views._apply_ticket_overwrites", return_value={}), \
             mock.patch("views.create_ticket", new=mock.AsyncMock(
                 return_value={"result": "created", "ticket": self._ticket()}
             )) as ct, \
             mock.patch("views.ticket_embed", return_value=mock.MagicMock()), \
             mock.patch("views.HTTicketView"), \
             mock.patch("views.set_panel_message", new=mock.AsyncMock()) as spm, \
             mock.patch("views.log_ticket_event", new=mock.AsyncMock()) as lte:
            asyncio.run(self.modal.on_submit(inter))

        for svc in (pt, he, fot, ct, spm, lte):
            self.assertEqual(svc.call_args.kwargs["session_factory"], factory,
                             f"{svc._mock_name} nedostal session_factory")

    def test_json_mode_sf_is_none(self):
        import asyncio
        inter = _interaction()
        inter.guild = self._guild()
        inter.followup.send = mock.AsyncMock()

        with mock.patch("views.player_tier", new=mock.AsyncMock(return_value="LT3")) as pt, \
             mock.patch("views.has_eval", new=mock.AsyncMock(return_value=True)), \
             mock.patch("views.tier_allows_tickets", return_value=True), \
             mock.patch("views.effective_ticket_tier", return_value="LT3E"), \
             mock.patch("views.next_ticket_tier", return_value="HT3"), \
             mock.patch("views.find_open_ticket", new=mock.AsyncMock(return_value=None)), \
             mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views._apply_ticket_overwrites", return_value={}), \
             mock.patch("views.create_ticket", new=mock.AsyncMock(
                 return_value={"result": "created", "ticket": self._ticket()}
             )), \
             mock.patch("views.ticket_embed", return_value=mock.MagicMock()), \
             mock.patch("views.HTTicketView"), \
             mock.patch("views.set_panel_message", new=mock.AsyncMock()), \
             mock.patch("views.log_ticket_event", new=mock.AsyncMock()):
            asyncio.run(self.modal.on_submit(inter))

        self.assertEqual(pt.call_args.kwargs["session_factory"], None)


if __name__ == "__main__":
    unittest.main()
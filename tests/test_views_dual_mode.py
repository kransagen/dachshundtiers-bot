"""Dual-mode testy views.py (Phase F #10c).

Ověřují, že interaktivní komponenty (JoinModal, QueueView, HT3PanelView,
HT3Modal) předávají ``session_factory`` do služeb a že JSON pre-checky
(closed fronta / cooldown / duplicita) běží přes duální služby
(queue_state / get_cooldowns / list_queue_entries) místo přímého čtení JSON
souborů.

Pull už tester roomku nevolí — patří kitu (`kit_tester_rooms`), takže
``QueueView.on_pull`` jde přes ``pull_for_kit`` stejně jako ``/queue pull``.

Diskutovaný MagicMock trap: u ``mock.MagicMock()`` interakce je
``interaction.client`` auto-MagicMock (truthy) => ``_session_factory`` by
tiše vrátil falešný session_factory a testy by šly po DB cestě. Všechny
testy proto používají ``interaction.client = None`` (JSON režim) nebo
explicitní fake session_factory (DB režim).
"""

import unittest
from unittest import mock

from views import (
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
        from services.queue_service import PULL_EMPTY, PullResult

        inter = _interaction()
        inter.user = mock.MagicMock()
        with mock.patch("views.has_tester_role", return_value=True), \
             mock.patch(
                 "views.pull_for_kit",
                 new=mock.AsyncMock(return_value=PullResult(status=PULL_EMPTY)),
             ):
            asyncio.run(self.view.on_pull(inter))
        inter.response.send_message.assert_called_once()
        self.assertIn("prázdná", inter.response.send_message.call_args.args[0])

    def test_pull_without_tester_room_keeps_player_in_queue(self):
        """No tester room -> tell the tester; never silently drop the player.

        The pull and the room lookup are one transaction, so a missing
        mapping means the queue entry is still ``waiting``.
        """
        import asyncio
        from services.queue_service import PULL_NO_ROOM, PullResult

        inter = _interaction()
        inter.user = mock.MagicMock()
        grant = mock.AsyncMock()
        with mock.patch("views.has_tester_role", return_value=True), \
             mock.patch(
                 "views.pull_for_kit",
                 new=mock.AsyncMock(return_value=PullResult(status=PULL_NO_ROOM)),
             ), \
             mock.patch("views.grant_pull_access", new=grant):
            asyncio.run(self.view.on_pull(inter))
        grant.assert_not_awaited()
        self.assertIn("mktesterroom", inter.response.send_message.call_args.args[0])


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

    def test_on_select_opens_ticket_without_modal(self):
        """Panel vybere jen kit a rovnou založí ticket – žádný modál, žádné psaní IGN/tieru."""
        import asyncio
        from services.ht3_tickets import HT3Context

        inter = _interaction()
        inter.guild = mock.MagicMock()
        inter.followup.send = mock.AsyncMock()
        inter.data = {"values": ["AnchorPvP"]}
        view = HT3PanelView(kits=["AnchorPvP"])
        opened = mock.AsyncMock(
            return_value=mock.MagicMock(status="opened", channel_id=5555, message=None)
        )
        context = HT3Context(
            ok=True,
            discord_id=99,
            ign="AliceMC",
            kit="AnchorPvP",
            current_tier="LT3",
            target_tier="HT3",
            eval_ok=True,
        )
        with mock.patch("views.get_cooldowns", new=mock.AsyncMock(
            return_value={"waitlist_ms": None, "ht3": {}}
        )), mock.patch(
            "views.resolve_ht3_context", new=mock.AsyncMock(return_value=context)
        ) as rhc, mock.patch("views._open_ht3_ticket", new=opened):
            asyncio.run(view.on_select(inter))

        inter.response.send_modal.assert_not_called()
        rhc.assert_awaited_once()
        self.assertEqual(rhc.await_args.args[1], "AnchorPvP")
        opened.assert_awaited_once()
        self.assertEqual(opened.await_args.kwargs["ign"], "AliceMC")
        self.assertEqual(opened.await_args.kwargs["target_tier"], "HT3")
        inter.followup.send.assert_awaited_once()
        self.assertIn("5555", inter.followup.send.await_args.args[0])

    def test_on_select_refuses_without_minecraft_link(self):
        import asyncio
        from services.ht3_tickets import HT3Context

        inter = _interaction()
        inter.guild = mock.MagicMock()
        inter.followup.send = mock.AsyncMock()
        inter.data = {"values": ["AnchorPvP"]}
        view = HT3PanelView(kits=["AnchorPvP"])
        opened = mock.AsyncMock()
        with mock.patch("views.get_cooldowns", new=mock.AsyncMock(
            return_value={"waitlist_ms": None, "ht3": {}}
        )), mock.patch("views.resolve_ht3_context", new=mock.AsyncMock(
            return_value=HT3Context(ok=False, reason="no_minecraft_account")
        )), mock.patch("views._open_ht3_ticket", new=opened):
            asyncio.run(view.on_select(inter))

        opened.assert_not_awaited()
        self.assertIn("/link", inter.followup.send.await_args.args[0])

    def test_on_select_refuses_without_tier(self):
        import asyncio
        from services.ht3_tickets import HT3Context

        inter = _interaction()
        inter.guild = mock.MagicMock()
        inter.followup.send = mock.AsyncMock()
        inter.data = {"values": ["AnchorPvP"]}
        view = HT3PanelView(kits=["AnchorPvP"])
        opened = mock.AsyncMock()
        with mock.patch("views.get_cooldowns", new=mock.AsyncMock(
            return_value={"waitlist_ms": None, "ht3": {}}
        )), mock.patch("views.resolve_ht3_context", new=mock.AsyncMock(
            return_value=HT3Context(ok=False, reason="no_current_tier")
        )), mock.patch("views._open_ht3_ticket", new=opened):
            asyncio.run(view.on_select(inter))

        opened.assert_not_awaited()
        self.assertIn("/result", inter.followup.send.await_args.args[0])

    def test_session_factory_propagated_to_context(self):
        import asyncio
        from services.ht3_tickets import HT3Context

        factory = object()
        inter = _interaction(db_factory=factory)
        inter.guild = mock.MagicMock()
        inter.followup.send = mock.AsyncMock()
        inter.data = {"values": ["AnchorPvP"]}
        view = HT3PanelView(kits=["AnchorPvP"])
        rhc = mock.AsyncMock(
            return_value=HT3Context(ok=False, reason="no_current_tier")
        )
        with mock.patch("views.get_cooldowns", new=mock.AsyncMock(
            return_value={"waitlist_ms": None, "ht3": {}}
        )), mock.patch("views.resolve_ht3_context", new=rhc):
            asyncio.run(view.on_select(inter))

        self.assertIs(rhc.await_args.kwargs["session_factory"], factory)


class OpenHT3TicketTests(unittest.TestCase):
    """``_open_ht3_ticket`` – jediné místo, kde se HT3 ticket opravdu založí.

    Pokrytá je i nešťastná větev: když ``create_ticket`` řekne „duplicate",
    právě vytvořený kanál se smaže, aby nezůstal sirotský pokoj bez ticketu.
    """

    def setUp(self):
        from views import _open_ht3_ticket

        self._open = _open_ht3_ticket

    def _interaction(self, db_factory=None):
        inter = _interaction(db_factory=db_factory)
        channel = mock.MagicMock()
        channel.id = 2001
        channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=3001))
        channel.delete = mock.AsyncMock()
        guild = mock.MagicMock()
        guild.get_member.return_value = mock.MagicMock()
        guild.get_channel.return_value = mock.MagicMock(id=4001)
        guild.create_text_channel = mock.AsyncMock(return_value=channel)
        inter.guild = guild
        inter.followup.send = mock.AsyncMock()
        return inter, channel

    def _ticket(self):
        return {
            "id": "2001", "status": "open", "ownerId": "99", "ownerName": "alice",
            "ign": "AliceMC", "kit": "AnchorPvP", "targetTier": "HT3",
            "currentTier": "LT3", "eval": True, "claimerId": None, "members": [],
        }

    def _call(self, inter, **overrides):
        import asyncio

        kwargs = dict(
            ign="AliceMC",
            kit="AnchorPvP",
            target_tier="HT3",
            current_tier="LT3",
            eval_ok=True,
            owner_id="99",
            session_factory=overrides.pop("session_factory", None),
        )
        kwargs.update(overrides)
        return asyncio.run(self._open(inter, **kwargs))

    def test_creates_channel_and_persists_ticket(self):
        inter, channel = self._interaction()
        with mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views._apply_ticket_overwrites", return_value={}), \
             mock.patch("views.create_ticket", new=mock.AsyncMock(
                 return_value={"result": "created", "ticket": self._ticket()}
             )) as ct, \
             mock.patch("views.ticket_embed", return_value=mock.MagicMock()), \
             mock.patch("views.HTTicketView"), \
             mock.patch("views.set_panel_message", new=mock.AsyncMock()) as spm, \
             mock.patch("views.log_ticket_event", new=mock.AsyncMock()) as lte:
            result = self._call(inter)

        self.assertEqual(result.status, "opened")
        self.assertEqual(result.channel_id, 2001)
        channel.delete.assert_not_awaited()
        self.assertEqual(ct.await_args.kwargs["channel_id"], 2001)
        self.assertEqual(ct.await_args.kwargs["target_tier"], "HT3")
        spm.assert_awaited_once()
        lte.assert_awaited_once()

    def test_session_factory_propagated_to_services(self):
        factory = object()
        inter, _ = self._interaction(db_factory=factory)
        with mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views._apply_ticket_overwrites", return_value={}), \
             mock.patch("views.create_ticket", new=mock.AsyncMock(
                 return_value={"result": "created", "ticket": self._ticket()}
             )) as ct, \
             mock.patch("views.ticket_embed", return_value=mock.MagicMock()), \
             mock.patch("views.HTTicketView"), \
             mock.patch("views.set_panel_message", new=mock.AsyncMock()) as spm, \
             mock.patch("views.log_ticket_event", new=mock.AsyncMock()) as lte:
            self._call(inter, session_factory=factory)

        for svc in (ct, spm, lte):
            self.assertIs(
                svc.await_args.kwargs["session_factory"], factory,
                f"{svc._mock_name} nedostal session_factory",
            )

    def test_duplicate_deletes_the_orphan_channel(self):
        inter, channel = self._interaction()
        existing = dict(self._ticket(), id="9999")
        with mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views._apply_ticket_overwrites", return_value={}), \
             mock.patch("views.create_ticket", new=mock.AsyncMock(
                 return_value={"result": "duplicate", "ticket": existing}
             )), \
             mock.patch("views.ticket_embed", return_value=mock.MagicMock()), \
             mock.patch("views.HTTicketView"), \
             mock.patch("views.set_panel_message", new=mock.AsyncMock()) as spm:
            result = self._call(inter)

        self.assertEqual(result.status, "duplicate")
        self.assertIsNone(result.channel_id)
        channel.delete.assert_awaited_once()
        spm.assert_not_awaited()
        self.assertIn("9999", result.message)

    def test_missing_category_creates_nothing(self):
        inter, _ = self._interaction()
        inter.guild.get_channel.return_value = None
        inter.guild.fetch_channel = mock.AsyncMock(return_value=None)
        created = mock.AsyncMock()
        with mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views.create_ticket", new=created):
            result = self._call(inter)

        self.assertEqual(result.status, "no_category")
        inter.guild.create_text_channel.assert_not_awaited()
        created.assert_not_awaited()

    def test_identity_conflict_is_reported(self):
        inter, channel = self._interaction()
        with mock.patch("views.get_ht3_ticket_category", return_value=4001), \
             mock.patch("views._apply_ticket_overwrites", return_value={}), \
             mock.patch("views.create_ticket", new=mock.AsyncMock(
                 return_value={
                     "result": "identity_conflict",
                     "message": "IGN patří jinému hráči",
                 }
             )):
            result = self._call(inter)

        self.assertEqual(result.status, "failed")
        channel.delete.assert_awaited_once()
        self.assertIn("jinému hráči", result.message)


if __name__ == "__main__":
    unittest.main()

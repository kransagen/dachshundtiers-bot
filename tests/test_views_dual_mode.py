"""Dual-mode testy views.py (Phase F #10c).

Ověřují, že interaktivní komponenty (QueueView, HT3PanelView,
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
    NOT_LINKED_MESSAGE,
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


class QueueJoinTests(unittest.TestCase):
    """Tlačítko Join Queue: žádný modál, propojené IGN, hlášky podle výsledku."""

    def setUp(self):
        self.view = QueueView("AnchorPvP")

    def _join(self, result):
        import asyncio
        inter = _interaction()
        with mock.patch("views.join_queue", new=mock.AsyncMock(return_value=result)) as jq, \
             mock.patch("views.update_panel", new=mock.AsyncMock()) as update_panel:
            asyncio.run(self.view.on_join(inter))
        return jq, update_panel, inter

    def test_passes_kit_and_session_factory(self):
        jq, _, _ = self._join({"result": "joined", "ign": "AliceMC"})
        self.assertEqual(jq.call_args.args[2], "AnchorPvP")
        self.assertIsNone(jq.call_args.kwargs["session_factory"])

    def test_joined_refreshes_panel_and_shows_ign(self):
        _, update_panel, inter = self._join({"result": "joined", "ign": "AliceMC"})
        update_panel.assert_awaited_once()
        self.assertIn("AliceMC", inter.response.send_message.call_args.args[0])
        inter.response.send_modal.assert_not_called()

    def test_not_linked_points_to_linkign(self):
        _, update_panel, inter = self._join({"result": "not_linked"})
        self.assertEqual(inter.response.send_message.call_args.args[0], NOT_LINKED_MESSAGE)
        update_panel.assert_not_called()

    def test_closed_cooldown_duplicate_messages(self):
        for result, needle in (
            ({"result": "closed"}, "zavřená"),
            ({"result": "cooldown", "remaining": 3_600_000}, "cooldown"),
            ({"result": "duplicate"}, "zapsaný"),
        ):
            _, update_panel, inter = self._join(result)
            self.assertIn(needle, inter.response.send_message.call_args.args[0])
            update_panel.assert_not_called()

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

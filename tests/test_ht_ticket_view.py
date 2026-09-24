"""Regresní testy HTTicketView (views.py).

Bugfix: ``HTTicketView._refresh(interaction, ticket)`` stínilo interní
``discord.ui.View._refresh(components)`` (volané synchronně z
``discord.ui.view.ViewStore.update_from_message`` při každém MESSAGE_UPDATE
na zprávě s persistent view). Discord při MESSAGE_UPDATE volá ``view._refresh(components)``
s JEDINÝM argumentem, takže naše async metoda s argumenty ``(interaction, ticket)``
spadla na ``TypeError: HTTicketView._refresh() missing 1 required positional
argument: 'ticket'`` a shodila celý bot.

Helper byl přejmenován na ``_refresh_ticket_state``; ``HTTicketView`` už
interní ``_refresh`` nepřepisuje a dědí ho z ``discord.ui.View``.
"""

import asyncio
import unittest
from unittest import mock

import discord
import discord.ui.view as discord_view_view  # ViewStore – přesně crash cesta

from views import HTTicketView


def _ticket(**overrides):
    """Minimální ticket (musí projít ticket_embed + handlers)."""
    ticket = {
        "id": "1001",
        "status": "open",
        "ownerId": "1",
        "ownerName": "alice",
        "ign": "AliceMC",
        "kit": "AnchorPvP",
        "targetTier": "HT3",
        "currentTier": "LT3",
        "eval": True,
        "claimerId": None,
        "members": [],
    }
    ticket.update(overrides)
    return ticket


def _interaction():
    inter = mock.MagicMock()
    inter.channel_id = 1001
    inter.user.id = "99"
    inter.user.display_name = "tester-one"
    inter.message = mock.MagicMock()
    inter.message.edit = mock.AsyncMock()
    inter.response.send_message = mock.AsyncMock()
    return inter


class HTTicketViewRegressionTests(unittest.TestCase):
    # ------------------------------------------------------------------
    # 1) HTTicketView lze instanciovat + persistentní tlačítka
    # ------------------------------------------------------------------
    def test_instantiate(self):
        view = HTTicketView()
        self.assertIsNone(view.timeout)  # persistent (přežije restart)
        self.assertEqual(
            [c.custom_id for c in view.children],
            ["ht_claim", "ht_unclaim", "ht_close", "ht_reopen"],
        )
        # View už NEstíní interní discord.py `_refresh` – dědí ho
        self.assertIs(HTTicketView._refresh, discord.ui.View._refresh)

    # ------------------------------------------------------------------
    # 2) Ticket-specific refresh (_refresh_ticket_state) stále funguje
    # ------------------------------------------------------------------
    def test_refresh_ticket_state_updates_embed(self):
        async def main():
            view = HTTicketView()
            inter = _interaction()
            await view._refresh_ticket_state(inter, _ticket())
            inter.message.edit.assert_awaited_once()
            embed = inter.message.edit.await_args.kwargs["embed"]
            self.assertIsInstance(embed, discord.Embed)

        asyncio.run(main())

    def test_refresh_ticket_state_swallows_edit_errors(self):
        async def main():
            view = HTTicketView()
            # HTTPException hierarchie – NotFound/Forbidden/HTTPException
            for exc in (
                discord.NotFound(mock.MagicMock(), "x"),
                discord.Forbidden(mock.MagicMock(), "x"),
                discord.HTTPException(mock.MagicMock(), "x"),
            ):
                inter = _interaction()
                inter.message.edit = mock.AsyncMock(side_effect=exc)
                await view._refresh_ticket_state(inter, _ticket())
                self.assertTrue(inter.message.edit.await_count >= 1)

            # žádný message object – nic se neděje, žádná chyba
            inter = _interaction()
            inter.message = None
            await view._refresh_ticket_state(inter, _ticket())

        asyncio.run(main())

    # ------------------------------------------------------------------
    # 3) discord.py cesta _refresh(components) nevyžaduje ticket
    # ------------------------------------------------------------------
    def test_view_refresh_components_does_not_require_ticket(self):
        # Přímé volání (synchronní, 1 argument) – přesně to dělá discord.py
        view = HTTicketView()
        self.assertIsNone(view._refresh([]))

        # Přesná cesta z crash tracebacku:
        # MessageUpdate → ViewStore.update_from_message → view._refresh(components)
        async def main():
            store = discord_view_view.ViewStore(mock.MagicMock())
            view2 = HTTicketView()
            store.add_view(view2, message_id=12345)
            store.update_from_message(12345, [])  # nesmí vyhodit TypeError

            # s reálným payloadem tlačítka (jak ho posílá Discord)
            payload = {
                "type": 2,
                "style": 3,
                "custom_id": "ht_claim",
                "label": "Claim HT",
            }
            store.update_from_message(12345, [payload])

        asyncio.run(main())

    # ------------------------------------------------------------------
    # 4) Tlačítka + callbacky stále fungují
    # ------------------------------------------------------------------
    def test_buttons_wired_and_callbacks_dispatch(self):
        async def main():
            view = HTTicketView()
            inter = _interaction()
            with mock.patch("views.has_tester_role", return_value=False):
                for btn in view.children:
                    await btn.callback(inter)
            # každý handler se spustil a poslal hlášku (tester gate)
            self.assertEqual(inter.response.send_message.await_count, 4)

        asyncio.run(main())

    def test_claim_callback_positive_path_refreshes_embed(self):
        """Claim přes tlačítko → přístup + audit + refresh embedu (end-to-end)."""
        async def main():
            view = HTTicketView()
            inter = _interaction()
            claimed = {**_ticket(), "claimerId": "99"}
            with mock.patch("views.has_tester_role", return_value=True), \
                 mock.patch("views.get_ticket", new=mock.AsyncMock(return_value=_ticket())), \
                 mock.patch(
                     "views.claim_ticket",
                     new=mock.AsyncMock(return_value={"result": "claimed", "ticket": claimed}),
                 ), \
                 mock.patch("views.grant_channel_access", new=mock.AsyncMock()), \
                 mock.patch("views.log_ticket_event", new=mock.AsyncMock()):
                await view.children[0].callback(inter)  # ht_claim

            inter.message.edit.assert_awaited_once()  # _refresh_ticket_state proběhl
            inter.response.send_message.assert_awaited_once()

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
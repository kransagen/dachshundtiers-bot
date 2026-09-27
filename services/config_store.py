"""Runtime konfigurace bota (Phase F, todo #5) — key→JSONB store.

Nahrazuje perzistenci ``queue_channels.json`` a
``ht3_panel_message.json`` v produkčních exekučních cestách (F10).
Dual-mode vzor: ``session_factory`` předán → PostgreSQL
(``bot_config`` přes BotConfigRepository), jinak původní JSON soubor.

- ``queue_channels`` — runtime ID kanálů fronty per kit (má přednost před
  env ``QUEUE_CHANNELS_JSON`` / defaulty; env jako konfigurace zůstává).
- ``ht3_panel_message`` — message_id + channel_id HT3+ panelu pro
  re-registraci persistentní view po restartu.
"""

from __future__ import annotations

from typing import Optional

from config import QUEUE_CHANNELS, QUEUE_CHANNELS_FILE
from db.repositories.sync_audit import BotConfigRepository
from db.services.session import transaction as db_transaction
from storage import load_data, save_data

QUEUE_CHANNELS_KEY = "queue_channels"
HT3_PANEL_KEY = "ht3_panel_message"
HT3_PANEL_MESSAGE_FILE = "ht3_panel_message.json"


async def get_queue_channel_id(
    kit_key: str, *, session_factory=None
) -> Optional[int]:
    """ID kanálu panelu fronty pro kit: runtime přepis > env/defaulty."""
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            extra = (
                await BotConfigRepository().get(
                    session, QUEUE_CHANNELS_KEY, {}
                )
                or {}
            )
    else:
        extra = load_data(QUEUE_CHANNELS_FILE, {})

    lowered = str(kit_key).lower()
    for key in (str(kit_key), lowered):
        if key in extra:
            try:
                return int(extra[key])
            except (TypeError, ValueError):
                pass
    return QUEUE_CHANNELS.get(lowered)


async def set_queue_channel_id(
    kit_key: str, channel_id: int, *, session_factory=None
) -> None:
    """Uloží/změní kanál panelu fronty pro kit (používá /addqchannel)."""
    value = int(channel_id)
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            repo = BotConfigRepository()
            extra = dict(
                await repo.get(session, QUEUE_CHANNELS_KEY, {}) or {}
            )
            extra[str(kit_key).lower()] = value
            await repo.set(session, QUEUE_CHANNELS_KEY, extra)
    else:
        extra = load_data(QUEUE_CHANNELS_FILE, {})
        extra[str(kit_key).lower()] = value
        save_data(QUEUE_CHANNELS_FILE, extra)


async def get_ht3_panel(*, session_factory=None) -> dict:
    """Panel HT3+ ({message_id, channel_id}); prázdný dict, když neexistuje."""
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            return dict(
                await BotConfigRepository().get(
                    session, HT3_PANEL_KEY, {}
                )
                or {}
            )
    return dict(load_data(HT3_PANEL_MESSAGE_FILE, {}))


async def set_ht3_panel(
    message_id: int, channel_id: int, *, session_factory=None
) -> None:
    """Uloží zprávu HT3+ panelu (používá /ht3panel)."""
    panel = {"message_id": str(message_id), "channel_id": str(channel_id)}
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            await BotConfigRepository().set(session, HT3_PANEL_KEY, panel)
    else:
        save_data(HT3_PANEL_MESSAGE_FILE, panel)
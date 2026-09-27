"""Runtime konfigurace bota — key→JSONB store (PostgreSQL, jediné úložiště).

- ``queue_channels`` — runtime ID kanálů fronty per kit (má přednost před
  env ``QUEUE_CHANNELS_JSON`` / defaulty; env jako konfigurace zůstává).
- ``ht3_panel_message`` — message_id + channel_id HT3+ panelu pro
  re-registraci persistentní view po restartu.
"""

from __future__ import annotations

from typing import Optional

from config import QUEUE_CHANNELS
from db.repositories.sync_audit import BotConfigRepository
from db.services.session import transaction as db_transaction

QUEUE_CHANNELS_KEY = "queue_channels"
HT3_PANEL_KEY = "ht3_panel_message"


async def get_queue_channel_id(
    kit_key: str, *, session_factory
) -> Optional[int]:
    """ID kanálu panelu fronty pro kit: runtime přepis > env/defaulty."""
    async with db_transaction(session_factory) as session:
        extra = (
            await BotConfigRepository().get(session, QUEUE_CHANNELS_KEY, {})
            or {}
        )

    lowered = str(kit_key).lower()
    for key in (str(kit_key), lowered):
        if key in extra:
            try:
                return int(extra[key])
            except (TypeError, ValueError):
                pass
    return QUEUE_CHANNELS.get(lowered)


async def set_queue_channel_id(
    kit_key: str, channel_id: int, *, session_factory
) -> None:
    """Uloží/změní kanál panelu fronty pro kit (používá /addqchannel)."""
    value = int(channel_id)
    async with db_transaction(session_factory) as session:
        repo = BotConfigRepository()
        extra = dict(await repo.get(session, QUEUE_CHANNELS_KEY, {}) or {})
        extra[str(kit_key).lower()] = value
        await repo.set(session, QUEUE_CHANNELS_KEY, extra)


async def get_ht3_panel(*, session_factory) -> dict:
    """Panel HT3+ ({message_id, channel_id}); prázdný dict, když neexistuje."""
    async with db_transaction(session_factory) as session:
        return dict(
            await BotConfigRepository().get(session, HT3_PANEL_KEY, {}) or {}
        )


async def set_ht3_panel(
    message_id: int, channel_id: int, *, session_factory
) -> None:
    """Uloží zprávu HT3+ panelu (používá /ht3panel)."""
    panel = {"message_id": str(message_id), "channel_id": str(channel_id)}
    async with db_transaction(session_factory) as session:
        await BotConfigRepository().set(session, HT3_PANEL_KEY, panel)
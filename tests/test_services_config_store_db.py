"""services/config_store.py — PostgreSQL only.

Ověřuje kontrakt nad ``bot_config`` (key→JSONB): queue channel přepis
(runtime > env/defaulty) a HT3+ panel roundtrip.
"""

from db.repositories.sync_audit import BotConfigRepository
from db.services.session import transaction
from services import config_store as cfg


# ---------------------------------------------------------------------------
# DB režim
# ---------------------------------------------------------------------------


async def test_queue_channel_override_roundtrip(session_factory, clean_db):
    assert await cfg.get_queue_channel_id("molepvp", session_factory=session_factory) is None
    await cfg.set_queue_channel_id(
        "AnchorPvP", 555, session_factory=session_factory
    )
    assert (
        await cfg.get_queue_channel_id("anchorpvp", session_factory=session_factory)
        == 555
    )
    async with transaction(session_factory) as session:
        stored = await BotConfigRepository().get(session, "queue_channels", {})
    assert stored == {"anchorpvp": 555}


async def test_queue_channel_override_replaces(session_factory, clean_db):
    await cfg.set_queue_channel_id("anchorpvp", 1, session_factory=session_factory)
    await cfg.set_queue_channel_id("anchorpvp", 2, session_factory=session_factory)
    assert (
        await cfg.get_queue_channel_id("anchorpvp", session_factory=session_factory)
        == 2
    )


async def test_queue_channel_env_fallback(session_factory, clean_db, monkeypatch):
    monkeypatch.setattr(cfg, "QUEUE_CHANNELS", {"molepvp": 777})
    assert (
        await cfg.get_queue_channel_id("molepvp", session_factory=session_factory)
        == 777
    )
    await cfg.set_queue_channel_id(
        "molepvp", 888, session_factory=session_factory
    )
    assert (
        await cfg.get_queue_channel_id("molepvp", session_factory=session_factory)
        == 888
    )


async def test_queue_channel_override_beats_env(session_factory, clean_db, monkeypatch):
    monkeypatch.setattr(cfg, "QUEUE_CHANNELS", {"anchorpvp": 1})
    await cfg.set_queue_channel_id("anchorpvp", 2, session_factory=session_factory)
    assert (
        await cfg.get_queue_channel_id("anchorpvp", session_factory=session_factory)
        == 2
    )


async def test_ht3_panel_default_empty(session_factory, clean_db):
    assert await cfg.get_ht3_panel(session_factory=session_factory) == {}


async def test_ht3_panel_roundtrip(session_factory, clean_db):
    await cfg.set_ht3_panel(123, 456, session_factory=session_factory)
    panel = await cfg.get_ht3_panel(session_factory=session_factory)
    assert panel == {"message_id": "123", "channel_id": "456"}


async def test_ht3_panel_replace(session_factory, clean_db):
    await cfg.set_ht3_panel(1, 2, session_factory=session_factory)
    await cfg.set_ht3_panel(3, 4, session_factory=session_factory)
    assert await cfg.get_ht3_panel(session_factory=session_factory) == {
        "message_id": "3",
        "channel_id": "4",
    }
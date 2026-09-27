"""DB (PostgreSQL) i JSON režim services/config_store.py — Phase F (todo #5).

Ověřuje dual-mode kontrakt nad ``bot_config`` (key→JSONB): queue channel
přepis (runtime > env/defaulty) a HT3+ panel roundtrip; JSON režim
(session_factory=None) píše do původních souborů.
"""

from pathlib import Path

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


# ---------------------------------------------------------------------------
# JSON režim (session_factory=None) — parity s legacy soubory
# ---------------------------------------------------------------------------


async def test_json_queue_channel_roundtrip(tmp_path: Path, monkeypatch):
    import json

    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert await cfg.get_queue_channel_id("molepvp") is None
    await cfg.set_queue_channel_id("MolePvP", 111)
    assert await cfg.get_queue_channel_id("molepvp") == 111
    assert json.loads((tmp_path / "queue_channels.json").read_text()) == {
        "molepvp": 111
    }


async def test_json_ht3_panel_roundtrip(tmp_path: Path, monkeypatch):
    import json

    import storage as storage_mod

    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)
    assert await cfg.get_ht3_panel() == {}
    await cfg.set_ht3_panel(987, 654)
    assert await cfg.get_ht3_panel() == {
        "message_id": "987",
        "channel_id": "654",
    }
    assert json.loads((tmp_path / "ht3_panel_message.json").read_text()) == {
        "message_id": "987",
        "channel_id": "654",
    }
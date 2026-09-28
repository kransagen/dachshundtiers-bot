"""services/queue_service.py už NEMÁ JSON režim – výhradně PostgreSQL.

Bývalé JSON testy (active_queues.json / queue.json / pulled_players.json /
cooldowns.json přes ``services.store`` a tempdir ``storage.DATA_DIR``) jsou
pryč společně s JSON větvemi. Produkční chování nad PostgreSQL je pokryté:

  - ``tests/test_services_queue_db.py``               – join/leave/pop/remove,
    cooldown, duplicita, pulled roundtrip, preset roomky,
  - ``tests/test_services_queue_lifecycle_db.py``     – open/close, testeri,
    removeq, /skip, panel.

Tenhle soubor teď hlídá jen to, že se JSON režim nevrátil: žádný fallback na
soubor, ``session_factory`` je povinný a čisté JSON helpery
(``cooldown_remaining`` / ``already_in_queue`` / ``make_entry`` /
``move_to_queue_end``) neexistují.
"""

import inspect
from pathlib import Path

import pytest

from services import queue_service

COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000  # 4 dny, stejně jako v config


def _ms() -> int:
    import time

    return int(time.time() * 1000)


@pytest.mark.asyncio
async def test_session_factory_is_required(tmp_path, monkeypatch):
    """Všechny veřejné operace vyžadují ``session_factory`` (žádný default)."""
    monkeypatch.setattr("storage.DATA_DIR", str(tmp_path))
    cases = (
        lambda: queue_service.join_queue(
            "1", "alice", "AliceMC", "AnchorPvP",
            joined_at_ms=_ms(), cooldown_ms=COOLDOWN_MS,
        ),
        lambda: queue_service.leave_queue("1", "AnchorPvP"),
        lambda: queue_service.pop_for_kit("anchorpvp"),
        lambda: queue_service.remove_by_player_id("1"),
        lambda: queue_service.preset_player_room({"id": "1"}, 123),
        lambda: queue_service.save_pulled_player({"id": "1"}, 123),
        lambda: queue_service.remove_pulled_player("1"),
        lambda: queue_service.open_queue("anchorpvp", "AnchorPvP", "1", "A"),
        lambda: queue_service.set_queue_panel("anchorpvp", 1, 2),
        lambda: queue_service.close_queue("anchorpvp"),
        lambda: queue_service.queue_state("anchorpvp"),
        lambda: queue_service.active_queues(),
        lambda: queue_service.list_queue_entries("anchorpvp"),
        lambda: queue_service.queue_snapshot(),
        lambda: queue_service.peek_first_player(),
        lambda: queue_service.join_queue_tester("anchorpvp", "1"),
        lambda: queue_service.leave_queue_tester("anchorpvp", "1"),
        lambda: queue_service.register_global_tester("1", "A"),
        lambda: queue_service.removeq("1"),
        lambda: queue_service.skip_player("1"),
        lambda: queue_service.panel_message_id("anchorpvp"),
        lambda: queue_service.set_tester_room("anchorpvp", 123),
        lambda: queue_service.resolve_tester_room("anchorpvp"),
        lambda: queue_service.clear_tester_room("anchorpvp"),
        lambda: queue_service.resolve_kit("anchorpvp"),
        lambda: queue_service.pull_for_kit("anchorpvp"),
    )
    for call in cases:
        with pytest.raises(TypeError):
            await call()

    # V tempdir nevznikla žádná JSON data (fronta se nikam file-uvn nepsala).
    assert list(Path(tmp_path).iterdir()) == []


@pytest.mark.asyncio
async def test_no_json_mode_anymore(tmp_path, monkeypatch):
    """Žádné JSON soubory front se nečtou ani nepíšou – ani po primingu."""
    import storage

    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    for name in ("active_queues.json", "queue.json", "cooldowns.json",
                 "pulled_players.json", "queue_messages.json", "testers.json"):
        (tmp_path / name).write_text("{}" if name.endswith(".json") else "[]")

    with pytest.raises(TypeError):
        await queue_service.join_queue(
            "1", "alice", "AliceMC", "AnchorPvP",
            joined_at_ms=_ms(), cooldown_ms=COOLDOWN_MS,
        )

    # Soubory se ani nepohnuly.
    assert (tmp_path / "active_queues.json").read_text() == "{}"
    assert (tmp_path / "queue.json").read_text() == "{}"
    assert (tmp_path / "cooldowns.json").read_text() == "{}"


def test_legacy_json_helpers_and_branches_removed():
    """Zdrojový regresní test: JSON cesta z queue_service.py je pryč."""
    src = inspect.getsource(queue_service)
    assert "if session_factory is not None:" not in src
    assert "cooldown_remaining" not in src
    assert "already_in_queue" not in src
    assert "make_entry" not in src
    assert "move_to_queue_end" not in src
    assert "from services.store import transaction" not in src
    # Žádné čtení/zápis json souborů front v produkčním kódu (docstring se
    # o nich zmiňuje jen v minulém čase).
    assert "load_data(" not in src
    assert "save_data(" not in src
    assert "tx.get(" not in src
    assert "tx.set(" not in src
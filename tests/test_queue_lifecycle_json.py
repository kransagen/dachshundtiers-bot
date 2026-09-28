"""Životní cyklus front (open/close, testeri, /skip, panel) – výhradně PostgreSQL.

Tyhle testy dřív pokrývaly JSON režim (session_factory=None) fronty
(active_queues.json / queue.json / queue_messages.json / testers.json /
pulled_players.json přes ``services.store``). JSON režim je zcela odebraný;
produkční chování nad PostgreSQL je pokryté
``tests/test_services_queue_lifecycle_db.py``.

Tenhle soubor teď hlídá jenom, že se JSON fallback nevrátil: lifecycle služby
vyžadují ``session_factory`` a nikdy nesahají do JSON souborů.
"""

import inspect
from pathlib import Path

import pytest

from services import queue_service as qsvc

COOLDOWN_MS = 4 * 24 * 60 * 60 * 1000


@pytest.mark.asyncio
async def test_lifecycle_requires_session_factory(tmp_path, monkeypatch):
    monkeypatch.setattr("storage.DATA_DIR", str(tmp_path))
    cases = (
        lambda: qsvc.open_queue("anchorpvp", "AnchorPvP", "111", "Opener"),
        lambda: qsvc.close_queue("anchorpvp"),
        lambda: qsvc.queue_state("anchorpvp"),
        lambda: qsvc.active_queues(),
        lambda: qsvc.list_queue_entries("anchorpvp"),
        lambda: qsvc.queue_snapshot(),
        lambda: qsvc.peek_first_player(),
        lambda: qsvc.join_queue_tester("anchorpvp", "222"),
        lambda: qsvc.leave_queue_tester("anchorpvp", "222"),
        lambda: qsvc.register_global_tester("777", "Seven"),
        lambda: qsvc.removeq("1"),
        lambda: qsvc.skip_player("1"),
        lambda: qsvc.set_queue_panel("anchorpvp", 555, 999),
        lambda: qsvc.panel_message_id("anchorpvp"),
    )
    for call in cases:
        with pytest.raises(TypeError):
            await call()
    assert list(Path(tmp_path).iterdir()) == []


def test_no_json_fallback_sources():
    """Zdrojový regresní test pro lifecycle funkce queue_service."""
    src = inspect.getsource(qsvc)
    assert "if session_factory is not None:" not in src
    assert "def _json_active_queues" not in src
    # Žádný JSON I/O: module docstring o JSONu mluví jen v minulém čase,
    # ale produkční kód nesmí na soubor SAHAT.
    assert "load_data(" not in src
    assert "save_data(" not in src
    assert "from services.store import transaction" not in src
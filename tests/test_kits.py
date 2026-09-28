"""Kanonické názvy kitů existují jen v ``services.kit_catalog`` (z PostgreSQL).

Bývalé ``utils.canonical_kit_name`` / ``utils.migrate_mode_keys`` (sync, ze
výchozí sady) přežily jen kvůli jedinému legacy volajícímu –
``services.results.apply_result_to_players`` (JSON cesta). Ta je pryč, takže
i tyto helpery jsou pryč: názvy kitů se dnes řeší výhradně kanonicky v
``services.kit_catalog`` nad PostgreSQL (pokryté v
``tests/test_services_kits_evals_roles_db.py``).

Tyhle regresní testy hlídají, že se do ``utils`` nevrátí souborové čtení
katalogu ani sync "canonical" helper mimo DB vrstvu.
"""

from __future__ import annotations

import inspect

import utils


def test_no_legacy_sync_kit_name_helpers_in_utils():
    src = inspect.getsource(utils)
    assert "def canonical_kit_name" not in src
    assert "def migrate_mode_keys" not in src
    # Žádné čtení souboru (volání open/load_data atd.) na jméno kitu.
    assert "load_data(" not in src
    assert "open(" not in src


def test_default_kits_still_exported_for_autocomplete_and_empty_catalog():
    from utils import DEFAULT_KITS

    assert "AnchorPvP" in DEFAULT_KITS
    assert isinstance(DEFAULT_KITS, list)
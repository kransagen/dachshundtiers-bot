"""Phase D final evidence: real ``data/`` import je idempotentní (×2, fresh DB).

Běh nad skutečnými produkčními JSON soubory (data/): první import naplní
dimensions/players/history/issues, druhý import na stejném DB NESMÍ nic
přidávat ani měnit (pouze audit marker). Při chybějícím ``data/`` se test
přeskočí (lokální vývoj).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from db.models import (
    BotConfig,
    Kit,
    MigrationImportIssue,
    Player,
    TierDefinition,
    TierHistory,
)

from services.phase_d.import_data import (
    BOT_CONFIG_IMPORT_KEY,
    import_json_data,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(), reason="produkční data/ nejsou k dispozici"
)


@pytest.fixture()
def _markers(session_factory, clean_db):
    return None


async def _counts(session_factory):
    async with session_factory() as session:
        return {
            "kits": (await session.execute(select(func.count()).select_from(Kit))).scalar_one(),
            "tiers": (
                await session.execute(
                    select(func.count()).select_from(TierDefinition)
                )
            ).scalar_one(),
            "players": (
                await session.execute(
                    select(func.count()).select_from(Player)
                )
            ).scalar_one(),
            "history": (
                await session.execute(
                    select(func.count()).select_from(TierHistory)
                )
            ).scalar_one(),
            "issues": (
                await session.execute(
                    select(func.count()).select_from(MigrationImportIssue)
                )
            ).scalar_one(),
            "markers": (
                await session.execute(
                    select(func.count())
                    .select_from(BotConfig)
                    .where(BotConfig.key == BOT_CONFIG_IMPORT_KEY)
                )
            ).scalar_one(),
        }


async def test_real_import_is_idempotent_twice(session_factory, clean_db):
    first = await import_json_data(session_factory, data_dir=DATA_DIR)
    assert first["kits_ensured"] > 0
    assert first["players_created"] > 0
    assert first["history_imported"] > 0

    counts_after_first = await _counts(session_factory)

    second = await import_json_data(session_factory, data_dir=DATA_DIR)
    assert second["players_created"] == 0
    assert second["history_imported"] == 0
    assert second["issues_recorded"] == 0
    assert second["history_skipped_existing"] >= counts_after_first["history"]

    counts_after_second = await _counts(session_factory)
    assert counts_after_second == counts_after_first
    assert counts_after_second["markers"] == 1
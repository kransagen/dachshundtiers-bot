"""Cooldowny hráčů (PostgreSQL, jediné úložiště).

Business rule: cooldowny jsou VŽDY per player + kit + type – kit_id je
součástí unique constraintu (``uq_cooldowns_kit``) pro waitlist i HT3, takže
cooldown na jednom kitu nikdy neblokuje jiný kit.

Jediná výjimka jsou LEGACY řádky s ``kit_id IS NULL`` z Phase D importu
zdrojového ``cooldowns.json``, který kit dimenzi vůbec neměl. Import se
pokouší kit odvodit z ``ht_results.json`` (legacy zapisoval cooldown ve stejné
transakci jako výsledek, který nese ``kit`` i ``timestamp``); při jednoznačné
shodě vznikne per-kit řádek, jinak řádek zůstává globální a má otevřený
``MigrationImportIssue`` (``cooldown_kit_unattributable`` /
``cooldown_kit_unknown_in_registry``). Takový řádek se nikdy nepřepisuje ani
nemaže: dál blokuje všechny kity, dokud sám nevyprší (max 4 dny od migrace).

Runtime cooldowny (``services/results.py``, ``services/topresult.py``,
``services/tickets.py``, ``services/edituser.py``) nikdy ``kit_id=None`` nezakládají
— hlídá to ``tests/test_cooldown_scope.py``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from db.repositories.cooldowns import (
    COOLDOWN_HT3,
    COOLDOWN_WAITLIST,
    CooldownRepository,
)
from db.repositories.kits import KitRepository
from db.repositories.players import PlayerRepository
from db.services.session import transaction as db_transaction


def _remaining_ms(expires_dt: datetime, now_ms: int) -> int | None:
    expires_ms = int(expires_dt.timestamp() * 1000)
    remaining = expires_ms - now_ms
    return remaining if remaining > 0 else None


async def get_waitlist_cooldown_ms(uid, kit_key: str, *, session_factory) -> int | None:
    """Zbývající waitlist cooldown hráče PRO TENTO KIT (nebo None).

    Zohledňuje i legacy globální řádek (viz docstring modulu) – ten blokuje
    každý kit, dokud nevyprší.
    """
    now_ms = int(time.time() * 1000)
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return None
        kit = await KitRepository().get_by_key(session, (kit_key or "").strip().lower())
        if kit is None:
            return None
        active = await CooldownRepository().get_active_waitlist(
            session,
            player_id=player.id,
            kit_id=kit.id,
            now=datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
        )
    if not active:
        return None
    return _remaining_ms(active[0].expires_at, now_ms)


async def get_cooldowns(uid, *, session_factory) -> dict:
    """Aktivní cooldowny hráče: ``{"waitlist": {kit: ms}, "ht3": {kit: ms},
    "waitlist_legacy_global_ms": ms|None}``.

    ``waitlist_legacy_global_ms`` je odděleně reportovaný pre-migrační
    globální cooldown (kit_id IS NULL, viz docstring modulu) – blokuje
    VŠECHNY kity, ale nezobrazuje se jako by patřil jednomu z nich.
    """
    now_ms = int(time.time() * 1000)
    now_dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(uid))
        if player is None:
            return {"waitlist": {}, "ht3": {}, "waitlist_legacy_global_ms": None}

        async def _rows(cooldown_type: str) -> list:
            return await CooldownRepository().get_active(
                session, player_id=player.id, cooldown_type=cooldown_type, now=now_dt
            )

        async def _per_kit_map(rows: list) -> dict[str, int]:
            out: dict[str, int] = {}
            for row in rows:
                if row.kit_id is None:
                    continue
                kit = await KitRepository().get_by_id(session, row.kit_id)
                if kit is None:
                    continue
                remaining = _remaining_ms(row.expires_at, now_ms)
                if remaining is not None:
                    out[kit.name] = remaining
            return out

        waitlist_rows = await _rows(COOLDOWN_WAITLIST)
        waitlist_map = await _per_kit_map(waitlist_rows)
        ht3_map = await _per_kit_map(await _rows(COOLDOWN_HT3))

        legacy_global_row = next(
            (row for row in waitlist_rows if row.kit_id is None), None
        )
        legacy_global_ms = (
            _remaining_ms(legacy_global_row.expires_at, now_ms)
            if legacy_global_row is not None
            else None
        )
    return {
        "waitlist": waitlist_map,
        "ht3": ht3_map,
        "waitlist_legacy_global_ms": legacy_global_ms,
    }

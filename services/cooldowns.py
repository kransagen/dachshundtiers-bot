"""Cooldowny hráčů (Phase F, todo #10) — dual-mode čtečka pro /cooldown.

Nahrazuje JSON čtení ``cooldowns.json`` + ``ht3_cooldowns.json`` v
``cogs/ht3.py`` (F10). DB režim: CooldownRepository nad tabulkou
``cooldowns`` (waitlist = COOLDOWN_WAITLIST s kit_id NULL; HT3 ticket
cooldowny = COOLDOWN_HT3 per kit). JSON režim = původní emisní soubory,
beze změny. Vrací ``{"waitlist_ms": ..., "ht3": {kit_display: ms}}``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from config import PLAYER_COOLDOWN_MS
from db.repositories.cooldowns import (
    COOLDOWN_HT3,
    COOLDOWN_WAITLIST,
    CooldownRepository,
)
from db.repositories.kits import KitRepository
from db.repositories.players import PlayerRepository
from db.services.session import transaction as db_transaction
from storage import load_data

COOLDOWNS_FILE = "cooldowns.json"
HT3_COOLDOWNS_FILE = "ht3_cooldowns.json"


def _remaining_ms(expires_dt: datetime, now_ms: int) -> int | None:
    expires_ms = int(expires_dt.timestamp() * 1000)
    remaining = expires_ms - now_ms
    return remaining if remaining > 0 else None


async def get_cooldowns(
    uid, *, session_factory=None, waitlist_cooldown_ms: int | None = None
) -> dict:
    """Aktivní cooldowny hráče: waitlist_ms (nebo None) + {kit: ms} pro HT3."""
    now_ms = int(time.time() * 1000)
    if session_factory is not None:
        async with db_transaction(session_factory) as session:
            player = await PlayerRepository().get_by_discord_id(
                session, int(uid)
            )
            if player is None:
                return {"waitlist_ms": None, "ht3": {}}
            waitlist = await CooldownRepository().get_active(
                session,
                player_id=player.id,
                cooldown_type=COOLDOWN_WAITLIST,
                kit_id=None,
                now=datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
            )
            waitlist_ms = (
                _remaining_ms(waitlist[0].expires_at, now_ms) if waitlist else None
            )
            ht3 = await CooldownRepository().get_active(
                session,
                player_id=player.id,
                cooldown_type=COOLDOWN_HT3,
                now=datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc),
            )
            ht3_map: dict[str, int] = {}
            for row in ht3:
                kit = await KitRepository().get_by_id(session, row.kit_id)
                if kit is None:
                    continue
                remaining = _remaining_ms(row.expires_at, now_ms)
                if remaining is not None:
                    ht3_map[kit.name] = remaining
        return {"waitlist_ms": waitlist_ms, "ht3": ht3_map}
    queue_cooldowns = load_data(COOLDOWNS_FILE, {}) or {}
    ht3_cooldowns = load_data(HT3_COOLDOWNS_FILE, {}) or {}
    window_ms = (
        waitlist_cooldown_ms
        if waitlist_cooldown_ms is not None
        else int(PLAYER_COOLDOWN_MS)
    )
    last = queue_cooldowns.get(str(uid))
    waitlist_ms = None
    if last is not None:
        try:
            remaining = window_ms - (now_ms - int(last))
            waitlist_ms = remaining if remaining > 0 else None
        except ValueError:
            waitlist_ms = None
    ht3_map = {}
    for kit, expires in (ht3_cooldowns.get(str(uid), {}) or {}).items():
        try:
            remaining = int(expires) - now_ms
        except (TypeError, ValueError):
            continue
        if remaining > 0:
            ht3_map[str(kit)] = remaining
    return {"waitlist_ms": waitlist_ms, "ht3": ht3_map}
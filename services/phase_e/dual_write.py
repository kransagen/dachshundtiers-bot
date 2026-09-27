"""Phase E, E1 — dual-write stabilization helpers.

JSON+PG dual-write is time-boxed (design §15.7). This module provides:

1. ``json_export_enabled`` — feature flag gate for the JSON export side of
   confirmed state; when False, no confirmed outcome is exported to
   players.json (legacy readers stay untouched).
2. ``watchdog_divergence`` — observe-only comparison of JSON current tiers
   (players.json ``modes``) versus the PostgreSQL mirror
   (``player_current_tiers``). Divergence is REPORTED, never "fixed":
   the watchdog cannot touch Discord or the database.

Pure module (no db/discord imports) so the gate and comparator are
unit-testable in isolation.
"""

from __future__ import annotations

from typing import Any

import config


def json_export_enabled() -> bool:
    return bool(getattr(config, "PHASE_E_JSON_EXPORT_ENABLED", True))


def watchdog_divergence(
    json_tiers: dict[str, dict[str, str]],
    pg_tiers: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Compare JSON current tiers vs PG mirror current tiers.

    ``json_tiers`` maps discord_id -> {kit: tier}; ``pg_tiers`` has the
    same shape (mirror, not authoritative). Returns:

    - only_json: (discord_id, kit) present in JSON but absent in PG
    - only_pg:   present in PG mirror but absent in JSON
    - mismatch:  same (discord_id, kit), different tier value
    """
    only_json: list[tuple[str, str, str]] = []
    only_pg: list[tuple[str, str, str]] = []
    mismatch: list[tuple[str, str, str, str]] = []
    for player_id, kits in json_tiers.items():
        for kit, tier in kits.items():
            pg_tier = pg_tiers.get(player_id, {}).get(kit)
            if pg_tier is None:
                only_json.append((player_id, kit, tier))
            elif pg_tier != tier:
                mismatch.append((player_id, kit, tier, pg_tier))
    for player_id, kits in pg_tiers.items():
        for kit, tier in kits.items():
            if kit not in json_tiers.get(player_id, {}):
                only_pg.append((player_id, kit, tier))
    return {
        "only_json": sorted(only_json),
        "only_pg": sorted(only_pg),
        "mismatch": sorted(mismatch),
        "divergent": bool(only_json or only_pg or mismatch),
    }
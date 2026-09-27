"""D5 — tri-source validation: Discord (observed) vs PG mirror vs players.json.

Read-only. Discord is the authority; JSON conflicts are reported for human
review, never auto-fixed, never used to change Discord. A row is flagged
``json_conflict`` when mirror agrees with Discord but players.json differs
(Discord wins), ``mirror_drift`` when mirror disagrees with the latest Discord
observation, and ``json_unverified`` when a JSON current state has no Discord
observation (player not linked — needs /linkdiscord).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Kit,
    Player,
    PlayerCurrentTier,
    SyncAction,
    SyncRun,
    TierDefinition,
)
from db.repositories.sync_audit import SYNC_ACTION_APPLIED

DISCORD_WINS = True
JSON_UNVERIFIED_REASON = (
    "Hráč nemá Discord pozorování (není propojený); JSON stav NEJSOU aktuální truth. "
    "Řešení: /linkdiscord + snímek (D4)."
)
CONFLICT_REASON = (
    "Nesoulad Discord vs players.json. Discord je autorita — konflikt se opraví "
    "jen explicitně (linkdiscord / promotion), nikdy automaticky."
)

CONFLICT_LIMIT = 100


def _read_json_list(data_dir: Path, name: str) -> Optional[list]:
    path = data_dir / name
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, list) else None


async def _latest_run(session: AsyncSession) -> Optional[SyncRun]:
    return (
        (
            await session.execute(
                select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )


async def build_validation_report(session_factory, *, data_dir: Union[str, Path]) -> dict:
    data_dir = Path(data_dir)
    players_json = _read_json_list(data_dir, "players.json")
    kits_json = _read_json_list(data_dir, "kits.json")
    if players_json is None:
        raise ValueError("players.json chybí – tri-source report vyžaduje vstup.")

    async with session_factory() as session:
        tiers = {
            row.code: row.id
            for row in (await session.execute(select(TierDefinition))).scalars()
        }
        players = {
            row.ign: row
            for row in (await session.execute(select(Player))).scalars()
        }
        kit_keys = {row.id: row.key for row in (await session.execute(select(Kit))).scalars()}
        mirror_rows = {
            (m.player_id, m.kit_id): m
            for m in (await session.execute(select(PlayerCurrentTier))).scalars()
        }
        latest_run = await _latest_run(session)
        discord_by_player: dict[int, dict[int, int]] = {}
        run_meta = None
        if latest_run is not None:
            run_meta = {
                "sync_run_id": latest_run.id,
                "command": latest_run.command,
                "status": latest_run.status,
                "started_at": latest_run.started_at.isoformat(),
            }
            for action in (
                await session.execute(
                    select(SyncAction).where(
                        SyncAction.sync_run_id == latest_run.id,
                        SyncAction.status == SYNC_ACTION_APPLIED,
                    )
                )
            ).scalars():
                if action.player_id is not None and action.kit_id is not None:
                    discord_by_player.setdefault(action.player_id, {})[action.kit_id] = (
                        action.tier_id
                    )

    rows: list[dict] = []
    summary: dict[str, int] = {}
    for record in players_json:
        ign = record.get("username")
        if not ign or ign not in players:
            continue
        player = players[ign]
        modes = record.get("modes") or {}
        for kit_key, json_code in modes.items():
            kit = None
            if kits_json is None or kit_key in (kits_json or []):
                for kit_id, key in kit_keys.items():
                    if key == kit_key:
                        kit = kit_id
                        break
            json_tier_id = tiers.get(str(json_code))
            discord_tier = discord_by_player.get(player.id, {}).get(kit)
            mirror = (
                mirror_rows.get((player.id, kit)) if kit is not None else None
            )
            mirror_tier = mirror.tier_id if mirror is not None else None
            discord_observed = (
                player.discord_id is not None and discord_tier is not None
            )

            if not discord_observed:
                status = "json_unverified"
            elif json_tier_id is None:
                status = "json_unknown_code"
            elif mirror_tier != discord_tier:
                status = "mirror_drift"
            elif json_tier_id != discord_tier:
                status = "json_conflict"
            else:
                status = "agree"

            summary[status] = summary.get(status, 0) + 1
            if status != "agree" and len(rows) < CONFLICT_LIMIT:
                rows.append(
                    {
                        "player_ign": ign,
                        "discord_linked": player.discord_id is not None,
                        "kit": kit_key,
                        "json_tier_code": str(json_code),
                        "json_tier_id": json_tier_id,
                        "discord_tier_id": discord_tier,
                        "mirror_tier_id": mirror_tier,
                        "status": status,
                        "reason": (
                            JSON_UNVERIFIED_REASON
                            if status == "json_unverified"
                            else CONFLICT_REASON
                        ),
                    }
                )

    unlinked_count = sum(
        1 for p in players.values() if p.discord_id is None
    )
    return {
        "discord_wins": DISCORD_WINS,
        "latest_sync_run": run_meta,
        "summary": summary,
        "unlinked_players": unlinked_count,
        "discrepancies": rows,
    }
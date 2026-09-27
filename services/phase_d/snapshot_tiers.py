"""D4 — Discord current-tier snapshot into the PG mirror (observe-only).

Wraps ``DiscordSyncService.sync_guild``: input is member objects with
``.id`` + ``.role_ids`` (simulated file or real guild scan). The tool NEVER
mutates Discord — it only reads roles and mirrors observations into
``player_current_tiers``; unknown players and ambiguous multi-tier roles are
recorded as sync anomalies, never guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from db.services.mirror_sync import DiscordSyncService, DiscordSyncOutcome

SNAPSHOT_COMMAND = "/snapshot-tiers (Phase D)"


@dataclass(frozen=True)
class SimulatedMember:
    id: int
    role_ids: tuple[int, ...]

    @classmethod
    def from_record(cls, record: dict) -> "SimulatedMember":
        member_id = int(record["id"])
        role_ids = tuple(int(role_id) for role_id in record.get("role_ids", []))
        return cls(id=member_id, role_ids=role_ids)


@dataclass(frozen=True)
class SnapshotReport:
    members_input: int = 0
    outcome: Optional[DiscordSyncOutcome] = None

    def as_dict(self) -> dict:
        outcome = self.outcome
        base = {
            "command": SNAPSHOT_COMMAND,
            "members_input": self.members_input,
            "discord_mutations": 0,
        }
        if outcome is None:
            base.update({"status": "no_run", "reason": "no members provided"})
            return base
        base.update(
            {
                "sync_run_id": outcome.sync_run_id,
                "status": outcome.status,
                "scanned_members": outcome.scanned_members,
                "observations_applied": outcome.observations_applied,
                "anomalies": outcome.anomalies,
                "unknown_members": outcome.unknown_members,
                "failed_members": outcome.failed_members,
                "unknown_roles": list(outcome.unknown_roles),
            }
        )
        return base


def load_members(path: Union[str, Path]) -> list[SimulatedMember]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("members file must be a JSON list of {id, role_ids}")
    return [SimulatedMember.from_record(record) for record in records]


async def run_snapshot(
    session_factory,
    *,
    members: list[SimulatedMember],
    observed_at: Optional[datetime] = None,
    triggered_by: Optional[int] = None,
    triggered_by_name: Optional[str] = None,
) -> SnapshotReport:
    observed_at = observed_at or datetime.now(timezone.utc)
    if not members:
        return SnapshotReport(members_input=0)

    outcome = await DiscordSyncService().sync_guild(
        session_factory,
        members=members,
        observed_at=observed_at,
        triggered_by=triggered_by,
        triggered_by_name=triggered_by_name,
        command=SNAPSHOT_COMMAND,
        mode="observe",
    )
    return SnapshotReport(
        members_input=len(members),
        outcome=outcome,
    )
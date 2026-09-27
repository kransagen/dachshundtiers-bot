"""Phase D — data migration + cutover tooling (docs/MIGRATION_DESIGN.md §19).

Authority model: Discord is the sole authority for current player tiers;
PostgreSQL is the persistent mirror + history/audit store; ``data/*.json``
becomes export/compatibility only during Phase D (never authoritative).

Subcommands (``python -m services.phase_d.cli --help``):

* ``backup``     — D1: hashed byte-for-byte backup of the JSON data directory
                   + DB markers (audit log / bot_config), no secrets written,
* ``restore``    — D1: verified restore of a hashed backup (Discord untouched;
                   rollback semantics per design §16),
* ``list-backups`` — D1: enumerate available backups,
* ``inventory``  — D1: migration inventory + report generator.
"""

from __future__ import annotations
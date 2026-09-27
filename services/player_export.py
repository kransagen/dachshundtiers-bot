"""DB → canonical players.json shape (Phase F, todo #10d/#11).

DEPRECATED (F4): ``players.json`` je export-only soubor. Žádná produkční
funkce ho nesmí číst pro stanovení aktuálních tierů, rolí, identity ani
promočního stavu. Jediné zapisovače jsou tento modul (canonical export
z PostgreSQL) a legacy operátorské repair toky (importdiscord / checkweb
apply v cogs/sync.py), které zůstávají čistě operátorskými nástroji.

Jediný zdroj canonical JSON tvaru hráče postaveného z PostgreSQL:

    {"username": ign, "discordId": str|None, "modes": {Kit.name: tier_code},
     "history": {Kit.name: [{"date": "dd.mm.yyyy", "tier": code}, ...]}}

- ``modes``  → aktuální zrcadlo tierů (mirror) JOIN kits/tier_definitions;
  klíče jsou DISPLAY názvy kitů (``Kit.name``), přesně jako legacy players.json
  (cog editor i web canonical počítají s case-insensitivním lookupem),
- ``history`` → ``tier_history`` (append-only), vzestupně podle ``changed_at``,
  datum ve formátu ``%d.%m.%Y`` (kompatibilní s legacy exporty),
- ``discordId`` → přítomen, jen když je Discord ID claimnuto (None → vynechán),
  shodně s chováním ``claim_ign`` v JSON režimu.

Použití: edituser DB režim (Krok B), web canonical v DB režimu (execute_player_edit)
a deterministický F5 exportér (#11, ``write_players_export``).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import Kit, Player, TierDefinition
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorRepository, TierHistoryRepository
from db.services.session import transaction as db_transaction
from storage import data_path, ensure_data_dir

DATE_FORMAT = "%d.%m.%Y"

DEFAULT_HISTORY_LIMIT = 500


async def _kit_tier_maps(
    session: AsyncSession,
    kit_ids: Optional[set[int]] = None,
    tier_ids: Optional[set[int]] = None,
) -> tuple[dict[int, Kit], dict[int, TierDefinition]]:
    kits: dict[int, Kit] = {}
    tiers: dict[int, TierDefinition] = {}
    if kit_ids:
        kit_rows = await session.execute(select(Kit).where(Kit.id.in_(kit_ids)))
        kits = {k.id: k for k in kit_rows.scalars()}
    if tier_ids:
        tier_rows = await session.execute(
            select(TierDefinition).where(TierDefinition.id.in_(tier_ids))
        )
        tiers = {t.id: t for t in tier_rows.scalars()}
    return kits, tiers


async def build_player_shape(
    session: AsyncSession,
    player: Player,
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
) -> dict:
    """Canonical tvar hráče z DB.

    History je vzestupně (poslední = nejnovější) — cog zobrazuje
    ``entries[-8:][::-1]``, stejně jako legacy players.json.
    """
    shape: dict = {"username": player.ign, "modes": {}, "history": {}}
    if player.discord_id is not None:
        shape["discordId"] = str(player.discord_id)

    mirrors = await MirrorRepository().list_current(session, player_id=player.id)
    kit_rows, tier_rows = await _kit_tier_maps(
        session,
        kit_ids={m.kit_id for m in mirrors},
        tier_ids={m.tier_id for m in mirrors},
    )
    modes = shape["modes"]
    for mirror in mirrors:
        kit = kit_rows.get(mirror.kit_id)
        tier = tier_rows.get(mirror.tier_id)
        if kit is None or tier is None:
            continue
        modes[kit.name] = tier.code

    history_rows = await TierHistoryRepository().list_for_player(
        session, player_id=player.id, limit=history_limit
    )
    if history_rows:
        kit_ids = {h.kit_id for h in history_rows}
        tier_ids = {h.tier_id for h in history_rows}
        kit_rows, tier_rows = await _kit_tier_maps(session, kit_ids, tier_ids)
        # list_for_player vrací DESC (nejnovější první) → vzestupně pro canonical
        history: dict[str, list[dict]] = {}
        for hist in sorted(history_rows, key=lambda h: h.changed_at):
            kit = kit_rows.get(hist.kit_id)
            tier = tier_rows.get(hist.tier_id)
            if kit is None or tier is None:
                continue
            history.setdefault(kit.name, []).append(
                {
                    "date": hist.changed_at.strftime(DATE_FORMAT),
                    "tier": tier.code,
                }
            )
        shape["history"] = history

    return shape


async def export_players(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[dict]:
    async with db_transaction(session_factory) as session:
        players = await PlayerRepository().list_all(session)
        out = []
        for player in players:
            out.append(await build_player_shape(session, player, history_limit=history_limit))
    return out


async def iter_export_players(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
) -> AsyncIterator[dict]:
    async with db_transaction(session_factory) as session:
        players = await PlayerRepository().list_all(session)
        for player in players:
            yield await build_player_shape(session, player, history_limit=history_limit)


def _write_export_file(file: str, players: list[dict]) -> None:
    """Atomický zápis do souboru mimo storage backend (vždy na disk).

    storage.save_data by s aktivním PostgreSQL backendem psala do JSONB
    blobu, ne do players.json – export musí mířit vždy na disk.
    """
    ensure_data_dir()
    path = data_path(file)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


async def write_players_export(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    file: str = "players.json",
) -> list[dict]:
    """Deterministický F5 exportér: PostgreSQL → canonical players.json.

    Pořadí hráčů je stabilní (``PlayerRepository.list_all`` = ORDER BY id),
    výstup je shodný s ``export_players`` (stejný zdroj DB, stejný tvar).
    Toto je jediný kanonický zapisovač players.json v produkci (F4).
    """
    players = await export_players(session_factory, history_limit=history_limit)
    _write_export_file(file, players)
    return players

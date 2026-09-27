"""Evaly hráčů — nad Evaluation (PostgreSQL, jediné úložiště).

Evaluační nárok „LT3 + eval" se sleduje přes Evaluation (player_id, kit_id,
granted_by, revoked_at). Eval vyžaduje existující Player + Kit řádek
(Evaluation.player_id / kit_id jsou FK NOT NULL); neznámý hráč/kit → False.
"""

from __future__ import annotations

from db.repositories.evaluations import EvaluationRepository
from db.repositories.kits import KitRepository
from db.repositories.players import PlayerRepository
from db.services.session import transaction as db_transaction


async def has_eval(ign: str, kit: str, *, session_factory) -> bool:
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_ign(session, ign)
        kit_row = await KitRepository().get_by_key(
            session, (kit or "").strip().lower()
        )
        if player is None or kit_row is None:
            return False
        return await EvaluationRepository().has_active(
            session, player_id=player.id, kit_id=kit_row.id
        )


async def set_eval(ign: str, kit: str, *, session_factory) -> bool:
    kit_key = (kit or "").strip().lower()
    if not (ign or "").strip() or not kit_key:
        return False
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_ign(session, ign)
        kit_row = await KitRepository().get_by_key(session, kit_key)
        if player is None or kit_row is None:
            return False
        if await EvaluationRepository().has_active(
            session, player_id=player.id, kit_id=kit_row.id
        ):
            return True
        await EvaluationRepository().grant(
            session, player_id=player.id, kit_id=kit_row.id
        )
    return True


async def unset_eval(ign: str, kit: str, *, session_factory) -> bool:
    kit_key = (kit or "").strip().lower()
    if not (ign or "").strip() or not kit_key:
        return False
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_ign(session, ign)
        kit_row = await KitRepository().get_by_key(session, kit_key)
        if player is None or kit_row is None:
            return False
        return await EvaluationRepository().revoke(
            session, player_id=player.id, kit_id=kit_row.id
        )
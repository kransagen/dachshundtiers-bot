"""Player repository — stable Discord ID identity, mutable IGN.

Semantics mirror ``services/player_identity`` (the legacy JSON rules are the
contract, reproduced here for PostgreSQL):

* ``discord_id`` is the **stable identity**; ``ign`` is mutable (rename).
* A record without ``discord_id`` is legacy/unclaimed; attaching a Discord ID
  **adopts** the record (same row keeps its history via ``player_id`` FKs) and
  NEVER merges two players.
* An IGN that belongs to another Discord ID is a conflict —
  :class:`PlayerIdentityError` is raised; no guessing, no auto-merge.
* Only :meth:`PlayerRepository.get_or_create_by_ign` creates records without
  a Discord ID (``source='migration'``); all runtime observations claim the
  identity via :meth:`claim_discord_id`.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Player

PLAYER_SOURCE_DISCORD = "discord"
PLAYER_SOURCE_MIGRATION = "migration"

# CLAIM_* / RESOLVE_* mirror services.player_identity (must not diverge).
CLAIM_CREATED = "created"
CLAIM_RENAMED = "renamed"
CLAIM_ADOPTED = "adopted"
CLAIM_UNCHANGED = "unchanged"
RESOLVE_DISCORD_ID = "discord_id"
RESOLVE_IGN = "ign"


class PlayerIdentityError(ValueError):
    """Identity claim/rename rejected (IGN belongs to another player)."""


def shell_ign(discord_id: int) -> str:
    """Zástupné IGN hráče, u kterého skutečné IGN zatím neznáme."""
    return f"discord-{int(discord_id)}"


def _norm(value: str) -> str:
    return (value or "").strip().lower()


class PlayerRepository:
    """CRUD + identity operations on ``players``.

    All methods take an explicit :class:`AsyncSession` — the caller owns the
    transaction boundary and the session's lifecycle.
    """

    async def get_by_id(self, session: AsyncSession, player_id: int) -> Optional[Player]:
        return await session.get(Player, player_id)

    async def get_by_discord_id(
        self, session: AsyncSession, discord_id: int
    ) -> Optional[Player]:
        if discord_id is None:
            return None
        result = await session.execute(
            select(Player).where(Player.discord_id == int(discord_id))
        )
        return result.scalar_one_or_none()

    async def get_by_ign(self, session: AsyncSession, ign: str) -> Optional[Player]:
        name = _norm(ign)
        if not name:
            return None
        result = await session.execute(
            select(Player).where(func.lower(Player.ign) == name)
        )
        return result.scalar_one_or_none()

    async def resolve(
        self,
        session: AsyncSession,
        *,
        discord_id: Optional[int] = None,
        ign: Optional[str] = None,
    ) -> tuple[Optional[Player], str]:
        """Find a player — Discord ID first, then IGN.

        Returns ``(player, source)`` or ``(None, "")`` exactly like
        ``services.player_identity.resolve_player``.
        """
        if discord_id:
            player = await self.get_by_discord_id(session, discord_id)
            if player is not None:
                return player, RESOLVE_DISCORD_ID
        if ign:
            player = await self.get_by_ign(session, ign)
            if player is not None:
                return player, RESOLVE_IGN
        return None, ""

    async def get_or_create_by_discord_id(
        self, session: AsyncSession, *, discord_id: int, ign: str
    ) -> Player:
        """Find by Discord ID or create a runtime player (source='discord').

        The new IGN must not belong to another player (DB unique on
        ``lower(ign)`` backs this up; a conflicting pre-check raises first).
        """
        player = await self.get_by_discord_id(session, discord_id)
        if player is not None:
            return player
        existing = await self.get_by_ign(session, ign)
        if existing is not None and existing.discord_id is None:
            # Adoption is an explicit identity decision; a blind "create"
            # must not silently adopt. Caller should use claim_discord_id.
            raise PlayerIdentityError(
                f"IGN `{ign}` patří neobsazenému záznamu; pro připojení Discord "
                "ID použij claim_discord_id."
            )
        player = Player(
            discord_id=int(discord_id),
            ign=(ign or "").strip(),
            source=PLAYER_SOURCE_DISCORD,
        )
        session.add(player)
        await session.flush()
        return player

    async def get_or_create_shell(self, session: AsyncSession, *, discord_id: int) -> Player:
        """Hráč podle Discord ID, jinak prázdný záznam s IGN ``discord-<id>``.

        Pro testery a akce v ticketech, kde IGN neznáme. Přezdívka ze serveru
        se jako IGN nepoužívá: kolidovala by s cizím IGN a hráč by si pak
        vlastní IGN nemohl propojit. Prázdný záznam se při ``/linkign``
        sloučí se záznamem skutečného IGN.
        """
        did = int(discord_id)
        player = await self.get_by_discord_id(session, did)
        if player is not None:
            return player
        player = Player(discord_id=did, ign=shell_ign(did), source=PLAYER_SOURCE_DISCORD)
        try:
            async with session.begin_nested():
                session.add(player)
                await session.flush()
        except IntegrityError:
            existing = await self.get_by_discord_id(session, did)
            if existing is None:
                raise
            return existing
        return player

    async def get_or_create_by_ign(
        self,
        session: AsyncSession,
        *,
        ign: str,
        source: str = PLAYER_SOURCE_MIGRATION,
    ) -> Player:
        """Find by IGN or create an unclaimed legacy record (discord_id NULL)."""
        player = await self.get_by_ign(session, ign)
        if player is not None:
            return player
        player = Player(ign=(ign or "").strip(), source=source)
        session.add(player)
        await session.flush()
        return player

    async def claim_discord_id(
        self, session: AsyncSession, *, discord_id: int, ign: str
    ) -> tuple[str, Player]:
        """Attach a Discord ID to a player without ever merging two records.

        Outcome constants: ``CLAIM_CREATED`` / ``CLAIM_RENAMED`` /
        ``CLAIM_ADOPTED`` / ``CLAIM_UNCHANGED``. Conflicts raise
        :class:`PlayerIdentityError`.

        M2 audit fix: this is read-then-write (SELECT to decide
        create/rename/adopt, then INSERT/UPDATE) with no row lock, so two
        concurrent claims of the same brand-new ``discord_id`` (or the same
        IGN) can both pass the SELECT checks and race on the INSERT/UPDATE —
        the loser previously surfaced a raw ``IntegrityError`` instead of the
        expected ``PlayerIdentityError``. That raw exception type is exactly
        what let a Discord-confirmed promotion silently skip the outbox
        wedge (C2 audit finding: only ``PlayerIdentityError`` was recognized
        as an identity-conflict outcome upstream). Each attempt now runs in
        a SAVEPOINT (``begin_nested``) so a unique-constraint loss rolls
        back only that attempt, not the caller's whole transaction, and is
        retried against the now-visible winning row — never creates a
        duplicate identity, never surfaces a bare ``IntegrityError`` for a
        race that is really just "someone else claimed it a moment earlier".
        """
        for _attempt in range(3):
            try:
                async with session.begin_nested():
                    return await self._claim_discord_id_once(
                        session, discord_id=discord_id, ign=ign
                    )
            except IntegrityError:
                continue
        # Exhausted retries under sustained contention — surface as the
        # documented identity-conflict type, never a raw DB exception.
        raise PlayerIdentityError(
            f"Přiřazení Discord ID `{discord_id}` k IGN `{ign}` selhalo "
            "kvůli souběžné operaci (zkuste to prosím znovu)."
        )

    async def _claim_discord_id_once(
        self, session: AsyncSession, *, discord_id: int, ign: str
    ) -> tuple[str, Player]:
        ign_clean = (ign or "").strip()
        did = int(discord_id)
        if not ign_clean:
            raise PlayerIdentityError("IGN je prázdné – nelze přiřadit identitu.")

        by_did = await self.get_by_discord_id(session, did)
        if by_did is not None:
            if _norm(by_did.ign) == _norm(ign_clean):
                return CLAIM_UNCHANGED, by_did
            if by_did.ign_linked_at is not None:
                # Hráč si IGN potvrdil sám (/linkign) – IGN napsané testerem
                # nebo převzaté z přezdívky ho nikdy nepřejmenuje.
                return CLAIM_UNCHANGED, by_did
            other = await self.get_by_ign(session, ign_clean)
            if other is not None and other.id != by_did.id:
                raise PlayerIdentityError(
                    f"IGN `{ign_clean}` patří jinému hráči (Discord ID "
                    f"`{other.discord_id or '?'}`) – sloučení se odmítá."
                )
            by_did.ign = ign_clean
            await session.flush()
            return CLAIM_RENAMED, by_did

        by_ign = await self.get_by_ign(session, ign_clean)
        if by_ign is None:
            player = Player(
                discord_id=did, ign=ign_clean, source=PLAYER_SOURCE_DISCORD
            )
            session.add(player)
            await session.flush()
            return CLAIM_CREATED, player
        if by_ign.discord_id is not None:
            raise PlayerIdentityError(
                f"IGN `{ign_clean}` patří jinému hráči (Discord ID "
                f"`{by_ign.discord_id}`) – operace se odmítá."
            )
        by_ign.discord_id = did
        await session.flush()
        return CLAIM_ADOPTED, by_ign

    async def rename_ign(
        self, session: AsyncSession, *, player_id: int, new_ign: str
    ) -> Player:
        """Rename IGN; conflicts with another player's IGN are rejected."""
        name = (new_ign or "").strip()
        if not name:
            raise PlayerIdentityError("IGN je prázdné – přejmenování odmítnuto.")
        player = await self.get_by_id(session, player_id)
        if player is None:
            raise PlayerIdentityError("Hráč neexistuje.")
        other = await self.get_by_ign(session, name)
        if other is not None and other.id != player_id:
            raise PlayerIdentityError(
                f"IGN `{name}` patří jinému hráči – přejmenování se odmítá."
            )
        player.ign = name
        await session.flush()
        return player

    async def list_all(self, session: AsyncSession) -> list[Player]:
        result = await session.execute(select(Player).order_by(Player.id))
        return list(result.scalars())
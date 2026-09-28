"""Minecraft identity repositories.

Concurrency notes
-----------------
* :meth:`MinecraftAccountRepository.get_or_create` is a real
  ``INSERT ... ON CONFLICT DO NOTHING`` + re-``SELECT``, not a
  "SELECT then INSERT". Two concurrent ``/link`` calls for the same UUID would
  otherwise both miss the SELECT and one would lose the race to a unique
  violation (or create a duplicate account if the constraint were absent).
* :meth:`PlayerLinkTokenRepository.consume` is a **conditional UPDATE**
  (``WHERE consumed_at IS NULL``) that reports how many rows it touched. That
  is what makes consumption single-use under concurrency: the second caller
  gets ``rowcount == 0`` and is told the code was already used, instead of
  quietly re-running the link.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MinecraftAccount, Player, PlayerLinkToken, normalize_uuid


class MinecraftAccountRepository:
    """Minecraft accounts, keyed by UUID (never by name)."""

    async def get_by_uuid(
        self, session: AsyncSession, uuid: str
    ) -> Optional[MinecraftAccount]:
        value = normalize_uuid(uuid)
        if not value:
            return None
        result = await session.execute(
            select(MinecraftAccount).where(MinecraftAccount.uuid == value)
        )
        return result.scalar_one_or_none()

    async def get_or_create(
        self, session: AsyncSession, *, uuid: str, name: Optional[str] = None
    ) -> Optional[MinecraftAccount]:
        """Return the account for ``uuid``, creating it if needed.

        Returns ``None`` for a malformed UUID — the caller must treat that as
        a validation error. The insert is race-safe (see module docstring).
        """
        value = normalize_uuid(uuid)
        if not value:
            return None
        existing = await self.get_by_uuid(session, value)
        if existing is not None:
            if name and existing.name != name:
                existing.name = name
                await session.flush()
            return existing

        await session.execute(
            pg_insert(MinecraftAccount)
            .values(uuid=value, name=name)
            .on_conflict_do_nothing(constraint="uq_minecraft_accounts_uuid")
        )
        await session.flush()
        return await self.get_by_uuid(session, value)

    async def set_name(
        self, session: AsyncSession, *, account_id: int, name: Optional[str]
    ) -> None:
        account = await session.get(MinecraftAccount, account_id)
        if account is None or account.name == name:
            return
        account.name = name
        await session.flush()


class PlayerIdentityRepository:
    """The one-to-one ``Player <-> MinecraftAccount`` link itself."""

    async def get_account_for_player(
        self, session: AsyncSession, *, player_id: int
    ) -> Optional[MinecraftAccount]:
        result = await session.execute(
            select(MinecraftAccount)
            .join(Player, Player.minecraft_account_id == MinecraftAccount.id)
            .where(Player.id == player_id)
        )
        return result.scalar_one_or_none()

    async def get_player_for_account(
        self, session: AsyncSession, *, account_id: int
    ) -> Optional[Player]:
        result = await session.execute(
            select(Player).where(Player.minecraft_account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def link(
        self, session: AsyncSession, *, player_id: int, account_id: int
    ) -> None:
        """Point ``player_id`` at ``account_id``.

        The UNIQUE constraint on ``players.minecraft_account_id`` is what makes
        this one-to-one; if the account is already linked to somebody else the
        database rejects the write and the caller reports the conflict.
        """
        player = await session.get(Player, player_id)
        if player is None:
            raise LookupError(f"Player {player_id} neexistuje.")
        player.minecraft_account_id = account_id
        await session.flush()

    async def unlink(self, session: AsyncSession, *, player_id: int) -> bool:
        player = await session.get(Player, player_id)
        if player is None or player.minecraft_account_id is None:
            return False
        player.minecraft_account_id = None
        await session.flush()
        return True


class PlayerLinkTokenRepository:
    """One-time, expiring link proofs."""

    async def issue(
        self,
        session: AsyncSession,
        *,
        player_id: int,
        code: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> PlayerLinkToken:
        row = PlayerLinkToken(
            player_id=player_id,
            code=code,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def supersede_live_for_player(
        self, session: AsyncSession, *, player_id: int, at: datetime
    ) -> int:
        """Consume any still-open token of this player, as ``superseded``.

        Re-running ``/link`` invalidates the previous code, so an old code
        that leaked cannot be used after the player asked for a fresh one.
        """
        result = await session.execute(
            update(PlayerLinkToken)
            .where(
                PlayerLinkToken.player_id == player_id,
                PlayerLinkToken.consumed_at.is_(None),
            )
            .values(consumed_at=at, rejection_reason="superseded")
        )
        await session.flush()
        return result.rowcount or 0

    async def get_live_for_player(
        self, session: AsyncSession, *, player_id: int
    ) -> Optional[PlayerLinkToken]:
        result = await session.execute(
            select(PlayerLinkToken)
            .where(
                PlayerLinkToken.player_id == player_id,
                PlayerLinkToken.consumed_at.is_(None),
            )
            .order_by(PlayerLinkToken.issued_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_live_by_code(
        self, session: AsyncSession, *, code: str
    ) -> Optional[PlayerLinkToken]:
        result = await session.execute(
            select(PlayerLinkToken).where(
                PlayerLinkToken.code == code,
                PlayerLinkToken.consumed_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def consume(
        self,
        session: AsyncSession,
        *,
        token_id: int,
        consumed_at: datetime,
        minecraft_account_id: Optional[int] = None,
        rejection_reason: Optional[str] = None,
    ) -> bool:
        """Atomically mark a token used. ``False`` means someone else won the race.

        The ``consumed_at IS NULL`` predicate is the entire single-use
        guarantee: two concurrent verification attempts both select the same
        live row, but only the first UPDATE matches a row, so only one caller
        ever gets ``True``.
        """
        result = await session.execute(
            update(PlayerLinkToken)
            .where(
                PlayerLinkToken.id == token_id,
                PlayerLinkToken.consumed_at.is_(None),
            )
            .values(
                consumed_at=consumed_at,
                minecraft_account_id=minecraft_account_id,
                rejection_reason=rejection_reason,
            )
        )
        await session.flush()
        return (result.rowcount or 0) > 0

    async def history(
        self, session: AsyncSession, *, player_id: int, limit: int = 20
    ) -> list[PlayerLinkToken]:
        result = await session.execute(
            select(PlayerLinkToken)
            .where(PlayerLinkToken.player_id == player_id)
            .order_by(PlayerLinkToken.issued_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

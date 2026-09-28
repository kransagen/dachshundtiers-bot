"""Discord ↔ Minecraft linking (``/link``, ``/unlink``, ``/linked``).

Design
------
A Minecraft account is identified by its **UUID**. ``Player.ign`` stays as the
display name the business logic already uses everywhere, but identity — "is
this the same person?" — is decided by ``players.minecraft_account_id`` →
``minecraft_accounts.uuid``, which the database keeps one-to-one.

The link is established through a **one-time, expiring token**:

1. ``/link`` (Discord side, caller already authenticated) issues a random
   code and stores it with a short expiry. Any previously live code for that
   player is consumed as ``superseded`` first, so a leaked older code is dead
   the moment a new one is issued.
2. The code must be presented from the Minecraft side before it can do
   anything. :func:`complete_link` is the single place that consumes a code,
   and it is written to be called by an authenticated Minecraft-side
   integration (server plugin / webhook), not from a Discord command.
3. Consumption is a conditional UPDATE guarded by ``consumed_at IS NULL``, so
   a replayed code is rejected by the database even if two verifications race.

There is deliberately **no** Discord command that accepts a raw UUID and links
it immediately. That would let anyone claim any account by typing its UUID,
which is not ownership proof — the whole point of the token flow.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError

from db.base import utcnow
from db.models import normalize_uuid
from db.repositories.identity import (
    MinecraftAccountRepository,
    PlayerIdentityRepository,
    PlayerLinkTokenRepository,
)
from db.repositories.players import PlayerRepository
from db.services.session import transaction as db_transaction

log = logging.getLogger("dachshundtiers")

# Short enough that a code sitting in a screenshot is useless by the time
# anyone reads it, long enough for a player to actually run a command.
LINK_TOKEN_TTL = timedelta(minutes=15)
_LINK_CODE_BYTES = 9  # 12 base32-ish chars — short enough to retype, long enough


class LinkError(Exception):
    """Linking failed; ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LinkStatus:
    """What ``/linked`` reports. ``uuid`` is only ever a masked preview."""

    linked: bool
    uuid: Optional[str] = None
    name: Optional[str] = None
    pending_code_expires_at: Optional[datetime] = None
    discord_id: Optional[int] = None
    ign: Optional[str] = None


def _new_code() -> str:
    """Unambiguous, case-insensitive code (no ``0``/``O``/``1``/``I``)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(_LINK_CODE_BYTES))


def _is_code_collision(err: IntegrityError) -> bool:
    """True only for a unique violation on ``player_link_tokens.code``.

    Distinguishing the constraint matters: the insert can also fail on the
    expiry CHECK or a foreign key, and those are programming errors that a
    retry would only turn into a misleading "code collision".
    """
    text = str(getattr(err, "orig", err))
    return "uq_link_token_code" in text or (
        "player_link_tokens" in text and "code" in text and "unique" in text.lower()
    )


def mask_uuid(uuid: str) -> str:
    """``abcd1234-…-7890`` — enough for a human to recognise, not to reuse."""
    value = normalize_uuid(uuid)
    if not value:
        return ""
    return f"{value[:8]}-…-{value[-4:]}"


async def start_link(
    discord_id: int, *, session_factory, ttl: timedelta = LINK_TOKEN_TTL
) -> str:
    """Issue a one-time link code for ``discord_id`` and return it.

    Idempotent in the sense that matters: calling it again returns a *new*
    code and invalidates the previous one, so there is never more than one
    live code per player.
    """
    now = utcnow()
    if ttl <= timedelta(0):
        # `expires_at > issued_at` is a DB CHECK, so a non-positive TTL would
        # surface as a raw IntegrityError from deep inside the repository. Fail
        # here with a reason the caller can report instead.
        raise LinkError("invalid_ttl", "Platnost kódu musí být kladná.")
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None:
            raise LinkError("unknown_player", "Hráč v databázi neexistuje.")
        tokens = PlayerLinkTokenRepository()
        await tokens.supersede_live_for_player(session, player_id=player.id, at=now)
        # The UNIQUE on `code` is the last line of defence against an
        # (astronomically unlikely) collision. Retry *only* that violation:
        # any other IntegrityError (a CHECK on expiry, a foreign key) means
        # the arguments are wrong, and retrying it would burn the whole budget
        # and then report a code collision that never happened.
        for _ in range(5):
            code = _new_code()
            try:
                # A nested (SAVEPOINT) block so a code collision rolls back
                # only the insert, not the supersede above it.
                async with session.begin_nested():
                    await tokens.issue(
                        session,
                        player_id=player.id,
                        code=code,
                        issued_at=now,
                        expires_at=now + ttl,
                    )
                return code
            except IntegrityError as err:
                if not _is_code_collision(err):
                    raise
                log.warning("Kolize kódu propojení, zkouším další.")
        raise LinkError("code_collision", "Nepodařilo se vytvořit kód, zkus to prosím znovu.")


async def link_status(discord_id: int, *, session_factory) -> LinkStatus:
    """Current link state + whether a live code is pending."""
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None:
            return LinkStatus(linked=False)
        identity = PlayerIdentityRepository()
        account = await identity.get_account_for_player(session, player_id=player.id)
        live = await PlayerLinkTokenRepository().get_live_for_player(
            session, player_id=player.id
        )
        return LinkStatus(
            linked=account is not None,
            uuid=account.uuid if account is not None else None,
            name=account.name if account is not None else None,
            pending_code_expires_at=live.expires_at if live is not None else None,
            discord_id=player.discord_id,
            ign=player.ign,
        )


async def unlink(discord_id: int, *, session_factory) -> bool:
    """Drop the link. The ``minecraft_accounts`` row is kept.

    Keeping the account row means a re-link does not have to re-prove UUID
    ownership, and the history of who used that UUID is not erased by
    unlinking. Every live link token for the player is invalidated too.
    """
    now = utcnow()
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None:
            return False
        identity = PlayerIdentityRepository()
        removed = await identity.unlink(session, player_id=player.id)
        await PlayerLinkTokenRepository().supersede_live_for_player(
            session, player_id=player.id, at=now
        )
        return removed


async def complete_link(
    code: str, minecraft_uuid: str, *, session_factory
) -> LinkStatus:
    """Consume a link code and link the Minecraft account to its player.

    Call this from the **Minecraft side** (a server plugin or webhook) — it is
    the ownership proof. Never expose it as a Discord command that takes a
    UUID from the invoker: knowing a UUID is not the same as owning it.

    Raises :class:`LinkError` with a stable ``code`` for every rejection.
    """
    normalized = normalize_uuid(minecraft_uuid)
    if not normalized:
        raise LinkError("invalid_uuid", "UUID není ve správném formátu.")

    now = utcnow()
    # A rejection is itself a record worth keeping ("someone tried to link a
    # UUID that is not theirs"), so the transaction must COMMIT the rejection
    # and only then raise. Raising inside the block would roll the marking
    # back and the audit trail would be empty exactly when it matters. The
    # error is therefore parked in ``rejection`` and raised after commit.
    rejection: Optional[LinkError] = None
    status: Optional[LinkStatus] = None

    async with db_transaction(session_factory) as session:
        tokens = PlayerLinkTokenRepository()
        token = await tokens.get_live_by_code(session, code=(code or "").strip())
        if token is None:
            # Nothing to mark: an unknown code has no row to annotate.
            rejection = LinkError("unknown_code", "Kód neexistuje nebo už byl použitý.")
        elif token.expires_at <= now:
            await tokens.consume(
                session,
                token_id=token.id,
                consumed_at=now,
                rejection_reason="expired",
            )
            rejection = LinkError("expired", "Kód vypršel. Spusť /link znovu.")
        else:
            accounts = MinecraftAccountRepository()
            account = await accounts.get_or_create(
                session, uuid=normalized, name=None
            )
            identity = PlayerIdentityRepository()
            owner = await identity.get_player_for_account(
                session, account_id=account.id
            )
            if owner is not None and owner.id != token.player_id:
                # Burn the code: a mismatched UUID is either a mistake or an
                # attempt to steal somebody's account, and either way this code
                # must not stay usable.
                await tokens.consume(
                    session,
                    token_id=token.id,
                    consumed_at=now,
                    rejection_reason="wrong_uuid",
                )
                rejection = LinkError(
                    "uuid_taken",
                    "Tento Minecraft účet je už propojený s jiným hráčem.",
                )
            elif not await tokens.consume(
                session,
                token_id=token.id,
                consumed_at=now,
                minecraft_account_id=account.id,
            ):
                # Single-use: the conditional UPDATE is the whole guarantee.
                # If two verifications race, the loser links nobody.
                rejection = LinkError("code_already_used", "Kód už byl použitý.")
            else:
                await identity.link(
                    session, player_id=token.player_id, account_id=account.id
                )
                # The account name is intentionally left as-is here: the UUID is
                # the identity, and the display name is refreshed by the
                # Minecraft side (see `set_account_name`), the only party that
                # can see it.
                player = await PlayerRepository().get_by_id(session, token.player_id)
                status = LinkStatus(
                    linked=True,
                    uuid=account.uuid,
                    name=account.name,
                    discord_id=player.discord_id if player is not None else None,
                    ign=player.ign if player is not None else None,
                )

    if rejection is not None:
        raise rejection
    return status  # type: ignore[return-value]


async def set_account_name(uuid: str, name: str, *, session_factory) -> bool:
    """Keep the display name current; the UUID stays the identity."""
    async with db_transaction(session_factory) as session:
        account = await MinecraftAccountRepository().get_by_uuid(session, uuid)
        if account is None:
            return False
        await MinecraftAccountRepository().set_name(
            session, account_id=account.id, name=(name or "").strip() or None
        )
        return True


async def resolve_ign(
    discord_id: int, *, session_factory
) -> Optional[str]:
    """The Minecraft IGN to use on a player's behalf.

    Resolution order, and why:

    1. the **linked** Minecraft account's name — this is the one the player
       proved they own;
    2. the stored ``players.ign`` — still the value the rest of the business
       logic uses, so a player who has not linked yet keeps working;
    3. ``None`` — the caller must refuse rather than guess.
    """
    if discord_id is None:
        return None
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None:
            return None
        account = await PlayerIdentityRepository().get_account_for_player(
            session, player_id=player.id
        )
        if account is not None and account.name:
            return account.name
        return player.ign or None

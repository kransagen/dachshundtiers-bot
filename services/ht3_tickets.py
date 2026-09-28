"""HT3+ ticket context: who may open one, and with what values.

Why this module exists separately from ``services/tickets.py``
-------------------------------------------------------------
``services/tickets.py`` still carries the legacy JSON branch for every
operation (the ``session_factory is None`` path), and it is the file that gets
deleted when the JSON backend goes away. New logic that must not be entangled
with that legacy code lives here, database-only from the start: a feature that
silently worked in two backends is exactly the thing that makes removing one
risky.

Business rules
--------------
The HT3 panel used to ask the player for their IGN and for the tier they were
challenging for. Both are now *derived*, never typed:

* **IGN** comes from the player's linked Minecraft account. Typing it let a
  player open a ticket for somebody else's account, and the stored IGN then
  disagreed with the account they actually proved they own.
* **current tier** comes from ``player_current_tiers`` — the Discord-confirmed
  mirror, which is the only authority for a player's current tier.
* **target tier** is one rung up the ladder from the player's *effective*
  tier, computed by the same :func:`next_ticket_tier` / :func:`effective_ticket_tier`
  helpers the interactive flow has always used. Nothing is invented here; the
  player simply no longer gets to ask for more than the ladder allows.

A missing Minecraft account or a missing current tier is a **refusal**, not a
fallback to the stored ``players.ign``. Guessing an identity is how tickets
end up attached to the wrong person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from db.repositories.evaluations import EvaluationRepository
from db.repositories.identity import PlayerIdentityRepository
from db.repositories.kits import KitRepository, TierDefinitionRepository
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorRepository
from db.repositories.tickets import TicketRepository
from db.services.session import transaction as db_transaction
from services.tickets import (
    _db_ticket_to_dict,
    effective_ticket_tier,
    next_ticket_tier,
    tier_allows_tickets,
)

# Refusal reasons (stable strings; the UI maps them to player-facing text).
REFUSE_NO_PLAYER = "no_player"
REFUSE_NO_KIT = "no_kit"
REFUSE_NO_MINECRAFT = "no_minecraft_account"
REFUSE_NO_TIER = "no_current_tier"
REFUSE_NO_EVAL = "no_eval"
REFUSE_TIER_LIMIT = "tier_limit"
REFUSE_ALREADY_OPEN = "already_open_ticket"


@dataclass(frozen=True)
class HT3Context:
    """Everything needed to open an HT3 ticket, resolved from the database."""

    ok: bool
    reason: Optional[str] = None
    discord_id: Optional[int] = None
    owner_name: Optional[str] = None
    ign: Optional[str] = None
    kit: Optional[str] = None
    current_tier: Optional[str] = None
    target_tier: Optional[str] = None
    eval_ok: bool = False
    open_ticket: Optional[dict] = None

    @property
    def message(self) -> str:
        """Player-facing explanation; never leaks internals."""
        return _REFUSAL_MESSAGES.get(self.reason or "", "❌ HT3+ ticket nejde vytvořit.")


_REFUSAL_MESSAGES = {
    REFUSE_NO_PLAYER: "❌ Tebe v databázi nemám. Napiš testerovi.",
    REFUSE_NO_KIT: "❌ Kit neznám.",
    REFUSE_NO_MINECRAFT: (
        "❌ Nemáš propojený Minecraft účet. Bez propojení účtu nejde otevřít "
        "HT3+ ticket – spusť `/link` a prokazuj vlastnictví kódem."
    ),
    REFUSE_NO_TIER: (
        "❌ Pro tenhle kit nemám u tebe uložený žádný tier, takže nevím, na co "
        "tě poslat. Nejdřív si ho nech zapsat přes `/result`."
    ),
    REFUSE_NO_EVAL: (
        "❌ **Bez evalu nelze otevřít HT3+ ticket!**\n"
        "Eval dostaneš, když **porazíš LT3 testera** (nebo když tvůj tester "
        "usoudí, že máš HT3 skill). Roli máš pořád LT3, ale eval ti otevře "
        "HT3+ tickety."
    ),
    REFUSE_TIER_LIMIT: "❌ Na tenhle tier z tvého současného žebříčku nemáš nárok.",
    REFUSE_ALREADY_OPEN: "❌ Už máš otevřený HT3+ ticket pro tenhle kit.",
}


async def _resolve_kit(session, kit_key: str):
    """Kit by business key, falling back to a case-insensitive display name.

    Same order as every other kit lookup in the codebase: ``Kit.key`` is the
    business key, but a pasted display name must not silently become "unknown
    kit".
    """
    repo = KitRepository()
    return await repo.get_by_key(session, kit_key.lower()) or await repo.get_by_name(
        session, kit_key
    )


async def _current_tier_code(session, *, player_id: int, kit_id: int) -> Optional[str]:
    """The player's current tier code for a kit, from the Discord-confirmed mirror.

    ``player_current_tiers`` is a mirror of what Discord has confirmed, so it
    has no independent authority and must never be derived from a result the
    bot merely wrote. Going through :class:`MirrorRepository` keeps that
    invariant in one place instead of open-coding the lookup at each call site.
    """
    mirror = await MirrorRepository().get_current(
        session, player_id=player_id, kit_id=kit_id
    )
    if mirror is None:
        return None
    tier_row = await TierDefinitionRepository().get_by_id(session, mirror.tier_id)
    return tier_row.code if tier_row is not None else None


async def resolve_ht3_context(
    discord_id: int, kit: str, *, session_factory
) -> HT3Context:
    """Resolve everything the HT3 panel needs for ``discord_id`` + ``kit``.

    Order of checks matters and is deliberate: identity first, then the
    player's tier, then idempotency, then the eval gate and the ladder limit.
    Each step is a refusal the player can act on, so they get the *first*
    real problem instead of a generic error.
    """
    if session_factory is None:
        # No silent fallback. A ticket is a Discord-visible object with audit
        # history; writing one without a database would be a second, invisible
        # source of truth for exactly the data the brief makes relational.
        return HT3Context(ok=False, reason=REFUSE_NO_PLAYER)

    kit_key = (kit or "").strip()
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None:
            return HT3Context(ok=False, reason=REFUSE_NO_PLAYER)

        kit_row = await _resolve_kit(session, kit_key)
        if kit_row is None:
            return HT3Context(ok=False, reason=REFUSE_NO_KIT, discord_id=player.discord_id)

        # 1) Identity: only a PROVED Minecraft account counts. `players.ign` is
        #    NOT an acceptable substitute — it is free text, and falling back
        #    to it is how a ticket ends up on somebody else's account.
        account = await PlayerIdentityRepository().get_account_for_player(
            session, player_id=player.id
        )
        ign = ((account.name or "").strip() or None) if account is not None else None
        if not ign:
            return HT3Context(
                ok=False, reason=REFUSE_NO_MINECRAFT, discord_id=player.discord_id
            )

        # 2) Current tier: the Discord-confirmed mirror, nothing else.
        current_tier = await _current_tier_code(
            session, player_id=player.id, kit_id=kit_row.id
        )
        if not current_tier:
            return HT3Context(
                ok=False, reason=REFUSE_NO_TIER, discord_id=player.discord_id
            )

        # 3) Already an open ticket for this kit? Checked BEFORE any side
        #    effect, so a repeated panel use never creates a second channel.
        open_rows = await TicketRepository().list_open(
            session, player_id=player.id, kit_id=kit_row.id
        )
        if open_rows:
            return HT3Context(
                ok=False,
                reason=REFUSE_ALREADY_OPEN,
                discord_id=player.discord_id,
                current_tier=current_tier,
                open_ticket=await _db_ticket_to_dict(session, open_rows[0]),
            )

        # 4) Eval gate + ladder limit — the pre-existing business rules.
        eval_ok = await EvaluationRepository().has_active(
            session, player_id=player.id, kit_id=kit_row.id
        )
        if not eval_ok and not tier_allows_tickets(current_tier):
            return HT3Context(
                ok=False,
                reason=REFUSE_NO_EVAL,
                discord_id=player.discord_id,
                current_tier=current_tier,
                eval_ok=False,
            )

        effective = effective_ticket_tier(current_tier, eval_ok)
        target_tier = next_ticket_tier(effective) if effective else None
        if not target_tier:
            return HT3Context(
                ok=False,
                reason=REFUSE_TIER_LIMIT,
                discord_id=player.discord_id,
                current_tier=current_tier,
                eval_ok=eval_ok,
            )

        return HT3Context(
            ok=True,
            discord_id=player.discord_id,
            owner_name=player.ign,
            ign=ign,
            kit=kit_row.name,
            current_tier=current_tier,
            target_tier=target_tier,
            eval_ok=eval_ok,
        )


@dataclass(frozen=True)
class EvalTicketRequest:
    """What ``/seteval`` should do about the HT3 ticket, decided in the DB."""

    needs_ticket: bool
    reason: Optional[str] = None
    discord_id: Optional[int] = None
    owner_name: Optional[str] = None
    ign: Optional[str] = None
    kit: Optional[str] = None
    current_tier: Optional[str] = None
    target_tier: Optional[str] = None
    open_ticket: Optional[dict] = None


async def ensure_eval_ticket(ign: str, kit: str, *, session_factory) -> EvalTicketRequest:
    """Decide whether granting an eval should also open an HT3 ticket.

    **Idempotent by construction.** The check is the same one the ticket
    insert enforces (``uq_tickets_open_player_kit``: one open ticket per
    player+kit), read here before any side effect, so re-running ``/seteval`` —
    or replaying it after a Discord timeout — reports ``needs_ticket=False``
    instead of opening a second ticket. The database constraint remains the
    final authority: a race between two concurrent grants is caught there and
    reported as "already open", never as a second ticket.

    This function deliberately does **not** create the Discord channel. That is
    a side effect that cannot be rolled back, so it happens in the cog, after
    this decision, and is undone if ``create_ticket`` then reports a duplicate.
    """
    if session_factory is None:
        return EvalTicketRequest(needs_ticket=False, reason="no_database")

    kit_key = (kit or "").strip()
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_ign(
            session, (ign or "").strip().lower()
        )
        if player is None:
            return EvalTicketRequest(needs_ticket=False, reason="no_player")
        kit_row = await _resolve_kit(session, kit_key)
        if kit_row is None:
            return EvalTicketRequest(needs_ticket=False, reason="no_kit")

        open_rows = await TicketRepository().list_open(
            session, player_id=player.id, kit_id=kit_row.id
        )
        if open_rows:
            return EvalTicketRequest(
                needs_ticket=False,
                reason="already_open",
                discord_id=player.discord_id,
                open_ticket=await _db_ticket_to_dict(session, open_rows[0]),
            )

        # An eval lifts the player to at most the HT3 rung (the ladder rule
        # for "LT3 + eval"), so the target is derived, never typed. No ladder
        # position (unknown/R tier) means we do not invent a target.
        current_tier = await _current_tier_code(
            session, player_id=player.id, kit_id=kit_row.id
        )
        target_tier = next_ticket_tier(effective_ticket_tier(current_tier, True))
        if not target_tier:
            return EvalTicketRequest(
                needs_ticket=False,
                reason="no_tier",
                discord_id=player.discord_id,
                current_tier=current_tier,
            )

        return EvalTicketRequest(
            needs_ticket=True,
            discord_id=player.discord_id,
            owner_name=player.ign,
            ign=player.ign,
            kit=kit_row.name,
            current_tier=current_tier,
            target_tier=target_tier,
        )

"""Retire a peak tier – pravidla, výpočet nároku a zápis.

Pravidla (prahy podle aktuálního tieru hráče v kitu):

==========  ==========================  ==========================
tier        dny na tieru                výhry v tier testech
==========  ==========================  ==========================
LT2 / HT2   60                          2
LT1 / HT1   90                          3
HT3         60 (jen peak)               –
==========  ==========================  ==========================

* **Retire** (``/retire``) smí jen LT2/HT2/LT1/HT1: splněné dny NEBO výhry.
* **Peak** se zapisuje automaticky (``sweep_peaks`` + kontrola po promotion);
  navíc HT3 stačí 60 dní. Peak se nikdy nemaže ani nesnižuje.
* **Výhra** = počítají se jen HT výsledky (``/topresult``, ``kind='ht_fight'``):
  hráč jako soupeř vyhrál HT fight kitu od doby, kdy má současný tier, a testovaný měl tier o jeden rank níže nebo stejný.

Discord role mění až cog; tahle vrstva jen počítá a zapisuje do databáze.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Kit,
    KitRole,
    Player,
    PlayerCurrentTier,
    PlayerPeakTier,
    Result,
    TierDefinition,
    TierHistory,
)
from db.repositories.kits import KitRepository
from db.repositories.players import PlayerRepository
from db.repositories.tiers import MirrorServiceRepository
from db.services.session import transaction
from db.tier_catalog import ensure_tier, is_retired_code, ladder_rank, retired_code

SOURCE_RETIRE = "retire"
PEAK_DAYS = "days"
PEAK_WINS = "wins"
PEAK_RETIRE = "retire"

# kód tieru → (dny, výhry nebo None = výhrami se nezískává)
RETIRE_RULES: dict[str, tuple[int, Optional[int]]] = {
    "LT2": (60, 2), "HT2": (60, 2), "LT1": (90, 3), "HT1": (90, 3),
}
PEAK_RULES: dict[str, tuple[int, Optional[int]]] = {"HT3": (60, None), **RETIRE_RULES}


class RetireRefused(Exception):
    """Retire nejde provést; ``str(err)`` je hláška pro hráče."""


@dataclass(frozen=True)
class Progress:
    kit_id: int
    kit_name: str
    tier_code: str
    tier_id: int
    since: datetime
    days: int
    wins: int
    days_needed: int
    wins_needed: Optional[int]

    @property
    def days_left(self) -> int:
        return max(0, self.days_needed - self.days)

    @property
    def wins_left(self) -> Optional[int]:
        if self.wins_needed is None:
            return None
        return max(0, self.wins_needed - self.wins)

    @property
    def by_days(self) -> bool:
        return self.days >= self.days_needed

    @property
    def by_wins(self) -> bool:
        return self.wins_needed is not None and self.wins >= self.wins_needed

    @property
    def eligible(self) -> bool:
        return self.by_days or self.by_wins

    @property
    def reason(self) -> Optional[str]:
        if self.by_days:
            return PEAK_DAYS
        if self.by_wins:
            return PEAK_WINS
        return None


@dataclass(frozen=True)
class RetirePlan:
    player_id: int
    kit_id: int
    kit_name: str
    tier_id: int
    tier_code: str
    retired_tier_id: int
    retired_tier_code: str
    revoke_discord_role: Optional[int]
    grant_discord_role: Optional[int]
    progress: Progress


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _tier_since(
    session: AsyncSession, mirror: PlayerCurrentTier
) -> datetime:
    """Od kdy má hráč současný tier (poslední změna v ``tier_history``)."""
    row = (
        await session.execute(
            select(TierHistory)
            .where(
                TierHistory.player_id == mirror.player_id,
                TierHistory.kit_id == mirror.kit_id,
            )
            .order_by(TierHistory.changed_at.desc(), TierHistory.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None and row.tier_id == mirror.tier_id:
        return row.changed_at
    return mirror.observed_at


async def count_wins(
    session: AsyncSession,
    *,
    player: Player,
    kit_id: int,
    rank: int,
    since: datetime,
) -> int:
    """Výhry hráče v HT fightech nad testovanými o rank níže či stejným."""
    testee = TierDefinition.__table__.alias("testee")
    if player.discord_id is None:
        return 0
    ht_win = and_(
        Result.kind == "ht_fight",
        Result.opponent_id == int(player.discord_id),
        Result.outcome == "Lost",
    )
    stmt = (
        select(func.count(Result.id))
        .join(testee, testee.c.id == Result.previous_tier_id)
        .where(
            Result.kit_id == kit_id,
            Result.recorded_at >= since,
            testee.c.rank.in_((rank - 1, rank)),
            ht_win,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def _progress(
    session: AsyncSession,
    player: Player,
    mirror: PlayerCurrentTier,
    rules: dict[str, tuple[int, Optional[int]]],
    now: datetime,
) -> Optional[Progress]:
    tier = await session.get(TierDefinition, mirror.tier_id)
    if tier is None or tier.code not in rules:
        return None
    kit = await session.get(Kit, mirror.kit_id)
    days_needed, wins_needed = rules[tier.code]
    since = await _tier_since(session, mirror)
    days = max(0, (now - since).days)
    wins = 0
    if wins_needed is not None:
        wins = await count_wins(
            session, player=player, kit_id=mirror.kit_id,
            rank=ladder_rank(tier.code), since=since,
        )
    return Progress(
        kit_id=mirror.kit_id, kit_name=kit.name, tier_code=tier.code,
        tier_id=tier.id, since=since, days=days, wins=wins,
        days_needed=days_needed, wins_needed=wins_needed,
    )


async def player_progress(
    session: AsyncSession, player: Player, *, now: Optional[datetime] = None
) -> list[Progress]:
    """Postup k peaku pro každý kit, kde má hráč tier s pravidlem."""
    now = now or _utcnow()
    mirrors = (
        await session.execute(
            select(PlayerCurrentTier).where(PlayerCurrentTier.player_id == player.id)
        )
    ).scalars().all()
    out = []
    for mirror in mirrors:
        prog = await _progress(session, player, mirror, PEAK_RULES, now)
        if prog is not None:
            out.append(prog)
    return sorted(out, key=lambda p: p.kit_name.lower())


async def player_peaks(session: AsyncSession, player: Player) -> list[tuple[str, str]]:
    """(kit, tier) všech zapsaných peaků hráče."""
    rows = (
        await session.execute(
            select(Kit.name, TierDefinition.code)
            .select_from(PlayerPeakTier)
            .join(Kit, Kit.id == PlayerPeakTier.kit_id)
            .join(TierDefinition, TierDefinition.id == PlayerPeakTier.tier_id)
            .where(PlayerPeakTier.player_id == player.id)
            .order_by(Kit.name)
        )
    ).all()
    return [(k, t) for k, t in rows]


async def record_peak(
    session: AsyncSession,
    *,
    player_id: int,
    kit_id: int,
    tier_id: int,
    reason: str,
    now: datetime,
) -> bool:
    """Zapíše peak, jen pokud je vyšší než stávající. ``True`` = změna."""
    tier = await session.get(TierDefinition, tier_id)
    existing = (
        await session.execute(
            select(PlayerPeakTier).where(
                PlayerPeakTier.player_id == player_id, PlayerPeakTier.kit_id == kit_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            PlayerPeakTier(
                player_id=player_id, kit_id=kit_id, tier_id=tier_id,
                reason=reason, achieved_at=now,
            )
        )
        await session.flush()
        return True
    current = await session.get(TierDefinition, existing.tier_id)
    if (tier.rank or 0) <= (current.rank or 0):
        return False
    existing.tier_id = tier_id
    existing.reason = reason
    existing.achieved_at = now
    await session.flush()
    return True


async def grant_due_peaks(
    session: AsyncSession, player: Player, *, now: Optional[datetime] = None
) -> list[Progress]:
    """Zapíše všechny peaky, které hráč právě splňuje; vrátí nově zapsané."""
    now = now or _utcnow()
    granted = []
    for prog in await player_progress(session, player, now=now):
        if prog.eligible and await record_peak(
            session, player_id=player.id, kit_id=prog.kit_id,
            tier_id=prog.tier_id, reason=prog.reason, now=now,
        ):
            granted.append(prog)
    return granted


async def sweep_peaks(session_factory, *, now: Optional[datetime] = None) -> int:
    """Projde všechny hráče s peak-relevantním tierem, vrátí počet nových peaků."""
    async with transaction(session_factory) as session:
        ids = (
            await session.execute(
                select(PlayerCurrentTier.player_id)
                .join(TierDefinition, TierDefinition.id == PlayerCurrentTier.tier_id)
                .where(TierDefinition.code.in_(tuple(PEAK_RULES)))
                .distinct()
            )
        ).scalars().all()
    total = 0
    for player_id in ids:
        async with transaction(session_factory) as session:
            player = await session.get(Player, player_id)
            if player is not None:
                total += len(await grant_due_peaks(session, player, now=now))
    return total


async def _kit_by_ref(session: AsyncSession, ref: str) -> Optional[Kit]:
    repo = KitRepository()
    return await repo.get_by_name(session, ref) or await repo.get_by_key(
        session, (ref or "").strip().lower()
    )


async def plan_retire(
    session_factory, discord_id: int, kit_ref: str, *, now: Optional[datetime] = None
) -> RetirePlan:
    """Ověří nárok na retire; ``RetireRefused`` s hláškou, pokud nejde."""
    now = now or _utcnow()
    async with transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None or player.ign_linked_at is None:
            raise RetireRefused("❌ Nejdřív se propoj přes `/linkign`.")
        kit = await _kit_by_ref(session, kit_ref)
        if kit is None:
            raise RetireRefused(f"❌ Kit **{kit_ref}** neexistuje.")
        mirror = (
            await session.execute(
                select(PlayerCurrentTier).where(
                    PlayerCurrentTier.player_id == player.id,
                    PlayerCurrentTier.kit_id == kit.id,
                )
            )
        ).scalar_one_or_none()
        tier = await session.get(TierDefinition, mirror.tier_id) if mirror else None
        if tier is None:
            raise RetireRefused(f"❌ V kitu **{kit.name}** nemáš žádný tier.")
        if is_retired_code(tier.code):
            raise RetireRefused(f"❌ V kitu **{kit.name}** už jsi retired (`{tier.code}`).")
        if tier.code not in RETIRE_RULES:
            raise RetireRefused(
                f"❌ Retire je možný jen z LT2, HT2, LT1 a HT1 – v kitu **{kit.name}** "
                f"máš **{tier.code}**."
            )
        prog = await _progress(session, player, mirror, RETIRE_RULES, now)
        if not prog.eligible:
            raise RetireRefused(
                f"❌ Na retire z **{tier.code}** ({kit.name}) ještě nemáš nárok – "
                f"zbývá **{prog.days_left} dní** (máš {prog.days}/{prog.days_needed}) "
                f"nebo **{prog.wins_left} výher** (máš {prog.wins}/{prog.wins_needed})."
            )
        retired = await ensure_tier(session, retired_code(tier.code))
        roles = {
            r.tier_id: r.discord_role_id
            for r in (
                await session.execute(select(KitRole).where(KitRole.kit_id == kit.id))
            ).scalars()
        }
        return RetirePlan(
            player_id=player.id, kit_id=kit.id, kit_name=kit.name, tier_id=tier.id,
            tier_code=tier.code, retired_tier_id=retired.id, retired_tier_code=retired.code,
            revoke_discord_role=roles.get(tier.id), grant_discord_role=roles.get(retired.id),
            progress=prog,
        )


async def commit_retire(session_factory, plan: RetirePlan, *, actor_name: str) -> None:
    """Zapíše retire do zrcadla + historie a peak. Volat PO změně rolí na Discordu."""
    now = _utcnow()
    async with transaction(session_factory) as session:
        await MirrorServiceRepository().apply_observation(
            session, player_id=plan.player_id, kit_id=plan.kit_id,
            tier_id=plan.retired_tier_id, observed_at=now, source=SOURCE_RETIRE,
            discord_role_id=plan.grant_discord_role, actor_name=actor_name,
            reason=f"/retire {plan.tier_code}",
        )
        await record_peak(
            session, player_id=plan.player_id, kit_id=plan.kit_id,
            tier_id=plan.tier_id, reason=PEAK_RETIRE, now=now,
        )

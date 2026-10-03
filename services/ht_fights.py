"""HT Fight průvodce /topresult – čistá logika (bez discord.py a bez DB).

Pravidla:
  - pro cílový tier T se hrají zápasy v sekci „tier těsně pod T“ a v sekci T,
    jen u HT3 (a níž) je to jediná sekce HT3,
  - FT (first to N) určuje kit, ne tier,
  - skóre se zadává z pohledu hráče (``4-1``); bez ``ff`` musí vítěz dosáhnout
    přesně FT, s ``ff`` (vzdání) stačí libovolné nižší skóre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.results import normalize_tier
from services.tickets import HT3_TIER_LADDER

_REAL_LADDER = [t for t in HT3_TIER_LADDER if t != "LT3E"]
_FIRST_SECTION_TIER = "HT3"

SCORE_PATTERN = re.compile(r"^(\d+)\s*[-:]\s*(\d+)(?:\s+(ff|vzdal))?$", re.IGNORECASE)

MAX_FIGHTS = 5  # modál Discordu má max. 5 polí


@dataclass(frozen=True)
class FightScore:
    player: int
    opponent: int
    forfeit: bool

    @property
    def outcome(self) -> str:
        return "Won" if self.player > self.opponent else "Lost"

    @property
    def text(self) -> str:
        return f"{self.player}-{self.opponent}" + (" ff" if self.forfeit else "")


@dataclass(frozen=True)
class Fight:
    tier: str
    opponent_id: str
    opponent_name: str
    score: FightScore


def fight_sections(target_tier: str) -> list[str]:
    """Sekce zápasů pro cílový tier: [tier pod cílem, cíl], u HT3 a níž jen [cíl]."""
    target = normalize_tier(target_tier)
    if target not in _REAL_LADDER:
        return []
    idx = _REAL_LADDER.index(target)
    if idx <= _REAL_LADDER.index(_FIRST_SECTION_TIER):
        return [target]
    return [_REAL_LADDER[idx - 1], target]


def parse_fight_score(raw: str, first_to: int) -> tuple[FightScore | None, str]:
    """Rozparsuje skóre z pohledu hráče; vrací ``(skóre, "")`` nebo ``(None, chyba)``."""
    match = SCORE_PATTERN.match((raw or "").strip())
    if match is None:
        return None, f"neplatné skóre `{(raw or '').strip()}` – použij např. `{first_to}-1`"
    player, opponent = int(match.group(1)), int(match.group(2))
    forfeit = match.group(3) is not None
    if player == opponent:
        return None, "remíza není možná"
    best = max(player, opponent)
    if forfeit:
        if best > first_to:
            return None, f"víc než FT{first_to}"
    elif best != first_to:
        return None, f"vítěz musí mít přesně {first_to} (FT{first_to}), jinak přidej `ff`"
    return FightScore(player, opponent, forfeit), ""


def missing_sections(sections: list[str], chosen: dict[str, list]) -> list[str]:
    """Sekce, ve kterých není vybrán žádný soupeř."""
    return [tier for tier in sections if not chosen.get(tier)]


def format_topresult_fights_message(
    *,
    player_id,
    ign: str,
    tier_status: str,
    kit: str,
    first_to: int,
    fights: list[Fight],
    previous_tier: str = "",
    new_tier: str = "",
    role_id,
) -> str:
    """Veřejná zpráva ve stylu serveru, zápasy seskupené po sekcích."""
    parts = [f"<@{player_id}> - {ign} - **{tier_status}** - {kit}"]
    seen: list[str] = []
    for fight in fights:
        if fight.tier not in seen:
            seen.append(fight.tier)
    for tier in seen:
        parts += ["", f"**{normalize_tier(tier)} Fighty (FT{first_to}):**"]
        for fight in fights:
            if fight.tier != tier:
                continue
            verb = "vyhrál" if fight.score.outcome == "Won" else "prohrál"
            parts.append(f"> {verb} {fight.score.text} <@{fight.opponent_id}>")
    if new_tier and normalize_tier(new_tier) != normalize_tier(previous_tier):
        parts += [
            "",
            f"**Postup: {normalize_tier(previous_tier)} → {normalize_tier(new_tier)}**",
        ]
    parts += ["", f"<@&{role_id}>"]
    return "\n".join(parts)

"""Žebříček top výsledků – /topresult (čistá logika, bez discord.py).

Žádná druhá databáze: žebříček se počítá VŽDY z kanonické ``data/players.json``
(jediný zdroj pravdy). Historie výsledků (``ht_results.json``) se k němu
nepřímo vztahuje jen přes kanonická data, která zapisuje, ale nic dalšího
udržovat nepotřebuje.

Řazení používá skutečnou tier hierarchii projektu (``HT3_TIER_LADDER``
z services/tickets):

    LT5 < HT5 < LT4 < HT4 < LT3 < LT3+eval < HT3 < LT2 < HT2 < LT1 < HT1

Pořadí hráče je dané jeho **nejlepším tierem** napříč kity:
  1. nejlepší tier (vyšší v žebříčku = lepší),
  2. počet kitů s tímto nejlepším tierem,
  3. celkový počet tier záznamů,
  4. username (abecedně, case-insensitive).

Varianty záznamů se normalizují („LT3 EVAL" → LT3E atd.). Tiery mimo žebříček
(turnajové S/A/B, R-tiery jako RLT2) se neskórují – takoví hráči se
v žebříčku neobjeví (jsou ve ``excluded``) a zobrazí se jen v přehledu.
Nechceme si vymýšlet pořadí, které hierarchie nezná.

Filtry: ``tier`` (nejlepší tier = přesně tento tier), ``limit`` (velikost
stránky) a ``page`` (číslování stránek – paginace „pokud je potřeba",
přes page parametry).
"""

import math

from services.tickets import HT3_TIER_LADDER

# Normalizace známých variant zápisu tierů (před řazením).
TIER_ALIASES = {
    "LT3 EVAL": "LT3E",
    "LT3 EVALUATION": "LT3E",
    "LT3+EVAL": "LT3E",
}

# Displejové názvy tierů (pro embed).
TIER_DISPLAY = {"LT3E": "LT3 + eval"}

DEFAULT_LIMIT = 10
MAX_LIMIT = 25


def normalize_tier(value) -> str:
    """Normalizuje tier (velikost písmen, whitespace, známé aliasy)."""
    raw = value
    if raw is None:
        return ""
    s = str(raw).strip().upper()
    return TIER_ALIASES.get(s, s)


def tier_display(value) -> str:
    """Displejový název tieru („LT3E" → „LT3 + eval")."""
    t = normalize_tier(value)
    return TIER_DISPLAY.get(t, t)


def tier_rank(value):
    """Index tieru v žebříčku (0 = nejhorší) nebo None, když není v žebříčku."""
    t = normalize_tier(value)
    if t in HT3_TIER_LADDER:
        return HT3_TIER_LADDER.index(t)
    return None


def validate_filter_tier(value) -> str | None:
    """Ověří filtr ``tier:...`` – vrací normalizovaný tier, nebo None (neplatný)."""
    t = normalize_tier(value)
    return t if t in HT3_TIER_LADDER else None


def _clean_players(players) -> list:
    """Kanonická players.json bez poškozených záznamů (objekt s username)."""
    out = []
    for p in (players or []):
        if not isinstance(p, dict):
            continue
        if not str(p.get("username", "") or "").strip():
            continue
        out.append(p)
    return out


def _sort_key(entry: dict) -> tuple:
    return (
        -entry["best_rank"],
        -entry["best_kits_count"],
        -entry["total"],
        str(entry["username"]).lower(),
    )


def build_ranking(players) -> dict:
    """Sestaví žebříček z kanonické players.json (nic nemění).

    Vrací::
        {
          "ranked": [
              {"username", "best_tier", "best_tier_display", "best_rank",
               "best_kits", "best_kits_count", "total",
               "kits": {kit: tier},  # normalizované tiery hráče
               "rank": int},
              ...
          ],
          "excluded": [username, ...],  # jen tiery mimo žebříček
          "total_players": int,
          "total_ranked": int,
        }
    """
    players = _clean_players(players)
    entries = []
    excluded = []
    for p in players:
        username = str(p.get("username", "") or "").strip()
        modes = p.get("modes") if isinstance(p.get("modes"), dict) else {}
        kits = {}
        for kit, tier in modes.items():
            t = normalize_tier(tier) if tier else None
            if t:
                kits[str(kit).strip()] = t

        ranked_kits = {
            k: t for k, t in kits.items() if tier_rank(t) is not None
        }
        if not ranked_kits:
            excluded.append(username)
            continue

        best_rank = max(tier_rank(t) for t in ranked_kits.values())
        best_tier = HT3_TIER_LADDER[best_rank]
        best_kits = sorted(
            k for k, t in ranked_kits.items() if tier_rank(t) == best_rank
        )
        entries.append(
            {
                "username": username,
                "best_tier": best_tier,
                "best_tier_display": TIER_DISPLAY.get(best_tier, best_tier),
                "best_rank": best_rank,
                "best_kits": best_kits,
                "best_kits_count": len(best_kits),
                "total": len(ranked_kits),
                # Všechny kity hráče (i turnajové S/A nebo R-tiery mimo
                # žebříček) – pro zobrazení per-hráče; řazení je jen z ladderu.
                "kits": dict(sorted(kits.items(), key=lambda kv: str(kv[0]).lower())),
                "rank": 0,
            }
        )

    entries.sort(key=_sort_key)
    # Stejné skóre (nejlepší tier + počet kitů + počet záznamů) → stejné místo;
    # v rámci skupiny rozhoduje abeceda, další pokračuje hned za nimi.
    rank = 0
    prev_key = None
    for idx, entry in enumerate(entries, start=1):
        key = _sort_key(entry)[:3]
        if key != prev_key:
            rank = idx
            prev_key = key
        entry["rank"] = rank

    return {
        "ranked": entries,
        "excluded": sorted(set(excluded), key=str.lower),
        "total_players": len(players),
        "total_ranked": len(entries),
    }


def filter_by_tier(entries: list, tier: str) -> list:
    """Filtruje seřazené hráče na ty s nejlepším tierem == ``tier``.

    ``tier`` musí projít ``validate_filter_tier`` (jinak vyvolá ValueError).
    """
    t = validate_filter_tier(tier)
    if t is None:
        raise ValueError(
            f"❌ Neplatný tier `{tier}`. Platné hodnoty: "
            f"{', '.join(HT3_TIER_LADDER)} (např. HT3, LT2)."
        )
    return [e for e in entries if e["best_tier"] == t]


def paginate(entries: list, limit: int = None, page: int = 1) -> dict:
    """Paginace seřazených záznamů („pokud je potřeba").

    Vrací::
        {"items": [...], "limit": int, "page": int, "total_pages": int,
         "total": int, "start": int, "end": int}
    """
    limit = int(limit) if limit is not None else DEFAULT_LIMIT
    limit = min(max(limit, 1), MAX_LIMIT)
    total = len(entries)
    total_pages = max(1, math.ceil(total / limit)) if total else 1
    page = min(max(int(page or 1), 1), total_pages)
    start = (page - 1) * limit
    end = start + limit
    return {
        "items": entries[start:end],
        "limit": limit,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "start": start,
        "end": end,
    }


def find_player(players, names) -> dict | None:
    """Najde hráče v players.json podle více kandidátů (case-insensitive).

    ``names`` = discord jména člena (display_name, name, nick, ...).
    """
    candidates = {
        str(n).strip().lower() for n in (names or []) if str(n or "").strip()
    }
    if not candidates:
        return None
    for p in _clean_players(players):
        if str(p.get("username", "")).strip().lower() in candidates:
            return p
    return None


def player_summary(ranking: dict, player: dict) -> dict:
    """Souhrn jednoho hráče pro /topresult @player.

    Vrací: username, rank (None = mimo žebříček), best_tier, total,
           kits (setříděné od nejlepšího tieru), excluded (bool).
    """
    username = str(player.get("username", "") or "").strip()
    entry = next(
        (e for e in ranking["ranked"] if e["username"].lower() == username.lower()),
        None,
    )
    if entry is not None:
        kits = sorted(
            entry["kits"].items(),
            key=lambda kv: (tier_rank(kv[1]) is None, -(tier_rank(kv[1]) or -1)),
        )
        return {
            "username": username,
            "rank": entry["rank"],
            "best_tier": entry["best_tier"],
            "best_tier_display": entry["best_tier_display"],
            "total": entry["total"],
            "kits": [(k, TIER_DISPLAY.get(v, v)) for k, v in kits],
            "excluded": False,
        }
    # Hráč je v players.json, ale nemá žádný tier ze žebříčku.
    modes = player.get("modes") if isinstance(player.get("modes"), dict) else {}
    kits = sorted(
        ((str(k).strip(), tier_display(v)) for k, v in modes.items() if v),
        key=lambda kv: str(kv[0]).lower(),
    )
    return {
        "username": username,
        "rank": None,
        "best_tier": None,
        "best_tier_display": None,
        "total": len(kits),
        "kits": kits,
        "excluded": True,
    }
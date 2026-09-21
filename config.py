"""Konfigurace bota.

Hodnoty se čtou z proměnných prostředí (podporován soubor ``.env``).
"""

import json
import os
from typing import Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _dict_env(name: str, default: dict) -> dict:
    """Načte mapu "klíč -> ID" z JSON proměnné prostředí (hodnoty převede na int)."""
    raw = os.getenv(name)
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return dict(default)
    if not isinstance(parsed, dict):
        return dict(default)
    merged = dict(default)
    for key, value in parsed.items():
        try:
            merged[str(key).strip()] = int(value)
        except (TypeError, ValueError):
            continue
    return merged


# --- Discord -------------------------------------------------------------
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

# Pokud je zadáno, příkazy se registrují jen na tomto serveru (rychlejší vývoj).
GUILD_ID: Optional[int] = _int_env("GUILD_ID", 0) or None

# --- Cooldowny (v ms, stejně jako v originále) ----------------------------
PLAYER_COOLDOWN_MS: int = 4 * 24 * 60 * 60 * 1000  # 4 dny mezi tier testy hráče
HT3_COOLDOWN_MS: int = 7 * 24 * 60 * 60 * 1000     # 7 dní mezi HT3+ tickety na kit

# --- Kanály / kategorie (původně natvrdo v kódu) --------------------------
HT3_PANEL_CHANNEL_ID: int = _int_env("HT3_PANEL_CHANNEL_ID", 1511738760377925833)
TOURNAMENT_RESULT_CHANNEL_ID: int = _int_env("TOURNAMENT_RESULT_CHANNEL_ID", 1505130493283405884)

# --- Výsledkové kanály rozdělené podle tieru (= originál) -------------------
RESULT_CHANNEL_LOWER: int = _int_env("RESULT_CHANNEL_LOWER", 1505130409632206888)
RESULT_CHANNEL_UPPER: int = _int_env("RESULT_CHANNEL_UPPER", 1505130493283405884)
TIERS_UPPER = {"HT3", "LT2", "HT2", "LT1", "HT1"}


def get_result_channel_id(tier: str) -> int:
    """Vrátí ID výsledkového kanálu podle tieru (HT3+ → UPPER, jinak LOWER)."""
    return RESULT_CHANNEL_UPPER if tier.strip().upper() in TIERS_UPPER else RESULT_CHANNEL_LOWER

# --- HT3+ ticket kategorie -------------------------------------------------
# Výchozí kategorie pro všechny HT3+ tickety.
HT3_TICKET_CATEGORY_ID: int = _int_env("HT3_TICKET_CATEGORY_ID", 1521515658448470066)

# Volitelné: kategorie HT3+ ticketů nastavitelné přes env.
# Pro KAŽDÝ kit můžeš mít vlastní kategorii – klíč je jméno kitu
# (randompot, anchorpvp, ironaxe, ...). Volitelně lze přidat i tier
# (HT3, LT2, HT2, LT1, HT1) jako záchytnou kategorii. Příklad:
#   HT3_TICKET_CATEGORIES_JSON={"randompot":"1521515658448470066","ironaxe":"123456789012345678"}
# Priorita shody: kit -> tier -> HT3_TICKET_CATEGORY_ID.
HT3_TICKET_CATEGORIES: dict[str, int] = _dict_env("HT3_TICKET_CATEGORIES_JSON", {})


def get_ht3_ticket_category(tier: str, kit: str) -> int:
    """Vrátí ID kategorie pro HT3+ ticket (kit, pak tier, pak default)."""
    kit_key = kit.strip().lower()
    for key, value in HT3_TICKET_CATEGORIES.items():
        if str(key).lower() == kit_key:
            return value
    tier_key = tier.strip().upper()
    for key, value in HT3_TICKET_CATEGORIES.items():
        if str(key).lower() == tier_key.lower():
            return value
    return HT3_TICKET_CATEGORY_ID


# --- Kanály panelů front (= originál KIT_CHANNELS) --------------------------
# Každý kit má svůj určený kanál. /openq vždy pošle panel tam (a purge-uje
# kanál), ne do kanálu, kde byl příkaz zadán. Lze přepsat QUEUE_CHANNELS_JSON:
#   QUEUE_CHANNELS_JSON={"randompot":"123456789012345678", ...}
_DEFAULT_QUEUE_CHANNELS = {
    "randompot": 1523299006925508748,
    "anchorpvp": 1523298945886064700,
    "ironaxe": 1511718468221665310,
    "uhcmace": 1511718680910627099,
    "goldsmp": 1511718136783573122,
    "netheritesword": 1505141558582710343,
    "shieldlesssmp": 1546900909223706705,
}
QUEUE_CHANNELS: dict[str, int] = _dict_env("QUEUE_CHANNELS_JSON", _DEFAULT_QUEUE_CHANNELS)

# --- Role testera ---------------------------------------------------------
# Stačí, aby název role OBSAHOVAL tento řetězec (case-insensitive).
TESTER_ROLE_FRAGMENT: str = os.getenv("TESTER_ROLE_FRAGMENT", "tester")

# --- GitHub synchronizace (volitelné) --------------------------------------
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER: str = os.getenv("GITHUB_OWNER", "adrison99")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "DachshundTiers")
GITHUB_FILE_PATH: str = os.getenv("GITHUB_FILE_PATH", "players.json")
"""Konfigurace bota.

Hodnoty se čtou z proměnných prostředí (podporován soubor ``.env``).
"""

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


# --- Discord -------------------------------------------------------------
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")

# Pokud je zadáno, příkazy se registrují jen na tomto serveru (rychlejší vývoj).
GUILD_ID: Optional[int] = _int_env("GUILD_ID", 0) or None

# --- Cooldowny (v ms, stejně jako v originále) ----------------------------
PLAYER_COOLDOWN_MS: int = 4 * 24 * 60 * 60 * 1000  # 4 dny mezi tier testy hráče
HT3_COOLDOWN_MS: int = 7 * 24 * 60 * 60 * 1000     # 7 dní mezi HT3+ tickety na kit

# --- Kanály / kategorie (původně natvrdo v kódu) --------------------------
HT3_PANEL_CHANNEL_ID: int = _int_env("HT3_PANEL_CHANNEL_ID", 1511738760377925833)
HT3_TICKET_CATEGORY_ID: int = _int_env("HT3_TICKET_CATEGORY_ID", 1521515658448470066)
TOURNAMENT_RESULT_CHANNEL_ID: int = _int_env("TOURNAMENT_RESULT_CHANNEL_ID", 1505130493283405884)

# --- Role testera ---------------------------------------------------------
# Stačí, aby název role OBSAHOVAL tento řetězec (case-insensitive).
TESTER_ROLE_FRAGMENT: str = os.getenv("TESTER_ROLE_FRAGMENT", "tester")

# --- GitHub synchronizace (volitelné) --------------------------------------
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER: str = os.getenv("GITHUB_OWNER", "adrison99")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "DachshundTiers")
GITHUB_FILE_PATH: str = os.getenv("GITHUB_FILE_PATH", "players.json")
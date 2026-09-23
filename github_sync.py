"""Serializovaná synchronizace ``players.json`` na GitHub (Contents API).

Opravuje konflikt zápisu (HTTP 409 – sha se mezitím změnil, protože zapsal
někdo jiný): při konfliktu se aktuální soubor znovu stáhne, merge funkce se
znovu aplikuje na čerstvá data a PUT se zopakuje (až ``MAX_PUSH_ATTEMPTS``).

Všechny push navíc sdílejí jeden lock, takže dvě naše vlastní synchronizace
(např. ``/result`` a ``/websync apply`` naráz) si navzájem nekonfliktují.

Merge funkce (``build_fn``) musí být idempotentní pro libovolný aktuální
seznam hráčů – kvůli opakování po konfliktu. Návratová hodnota je nový
(popř. upravený) seznam.
"""

import asyncio
import base64
import json
import logging

import requests

from config import GITHUB_FILE_PATH, GITHUB_OWNER, GITHUB_REPO, GITHUB_TOKEN

log = logging.getLogger("dachshundtiers")

MAX_PUSH_ATTEMPTS = 3

# loop id -> asyncio.Lock (serializace všech GitHub push v jednom loopu)
_github_lock_registry: "dict[int, asyncio.Lock]" = {}


def _github_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = id(loop)
    lock = _github_lock_registry.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _github_lock_registry[key] = lock
    return lock


def _api_url() -> str:
    return (
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/contents/{GITHUB_FILE_PATH}"
    )


def _headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def _get_players_data():
    """Stáhne aktuální ``players.json`` z GitHubu (synchronně, v threadu).

    Vrací ``(players, sha, error)``:
      - 200          → (list, sha, None)
      - 404          → ([], None, None) – soubor na webu zatím není
      - jiné / chyba → (None, None, zpráva)
    """
    try:
        response = requests.get(_api_url(), headers=_headers(), timeout=20)
    except requests.RequestException as err:
        return None, None, f"❌ GitHub GET selhal: {err}"

    if response.status_code == 200:
        try:
            data = response.json()
            players = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            if isinstance(players, list):
                return players, data.get("sha"), None
        except (json.JSONDecodeError, ValueError):
            return None, None, "❌ GitHub vrací poškozený JSON"
        return None, None, f"GitHub GET selhal ({response.status_code})"

    if response.status_code == 404:
        return [], None, None

    return None, None, f"GitHub GET selhal ({response.status_code})"


def _put_players(players: list, sha, message: str):
    """Jeden pokus o PUT. Vrací ``(ok, status)`` – status je HTTP kód (int)
    nebo řetězec s chybou sítě."""
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(players, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    try:
        response = requests.put(_api_url(), headers=_headers(), json=body, timeout=20)
    except requests.RequestException as err:
        return False, str(err)
    return response.status_code in (200, 201), response.status_code


async def push_players(
    message: str,
    build_fn,
    *,
    success_message: str = "✅ players.json synchronizováno na GitHub – web je aktuální.",
    max_attempts: int = None,
):
    """Serializovaný push se znovupokusem při konfliktu (409).

    ``build_fn(players) -> players`` aplikuje naši změnu na aktuální seznam
    hráčů (musí být idempotentní). Vrací ``(ok, message, last_built)``:
      - ``last_built`` je poslední sloučený seznam (``None``, když merge
        nikdy neběžel – bez tokenu) – volající si ho může uložit lokálně.
    """
    attempts = max_attempts or MAX_PUSH_ATTEMPTS

    if not GITHUB_TOKEN:
        return (
            False,
            "⚠️ GITHUB_TOKEN není nastaven – uloženo jen lokálně (web se nezmění).",
            None,
        )

    async with _github_lock():
        last_built = None
        for _ in range(attempts):
            players, sha, error = await asyncio.to_thread(_get_players_data)
            if players is None:
                return False, error, None

            try:
                last_built = build_fn(players)
            except Exception as err:  # noqa: BLE001
                log.exception("Chyba v merge funkci GitHubu: %s", err)
                return False, f"❌ Nastala chyba při zápisu na GitHub: {err}", None

            ok, status = await asyncio.to_thread(_put_players, last_built, sha, message)
            if ok:
                return True, success_message, last_built
            if isinstance(status, str):
                return False, f"❌ GitHub zápis selhal: {status}", last_built
            if status != 409:
                return False, f"❌ GitHub zápis selhal ({status})", last_built
            # 409 – mezitím zapsal někdo jiný: stáhnout čerstvá data a sloučit znovu

        return (
            False,
            f"❌ GitHub zápis selhal (409 – konflikt přetrvává i po {attempts} pokusech)",
            last_built,
        )


async def fetch_players():
    """Stáhne aktuální ``players.json`` z GitHubu (jen čtení, bez zámku).

    Vrací ``(players, sha, error)``; error je ``None`` při úspěchu. Bez tokenu
    vrací ``(None, None, None)`` – volající má spadnout na lokální kopii.
    """
    if not GITHUB_TOKEN:
        return None, None, None
    players, sha, error = await asyncio.to_thread(_get_players_data)
    return players, sha, error
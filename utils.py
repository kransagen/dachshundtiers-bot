"""Drobné pomocné funkce sdílené napříč cogami."""

import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands

from services.permissions import has_tester_role as _permissions_has_tester_role

_log = logging.getLogger("dachshundtiers")

# Silné reference na úlohy na pozadí – samotný ``asyncio.create_task`` drží
# jen slabou referenci, takže by úlohu mohl uklidit GC a výjimka by zmizela.
_background_tasks: "set[asyncio.Task]" = set()


def _task_done(task: "asyncio.Task") -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _log.error("Úloha na pozadí %s selhala", task.get_name(), exc_info=task.exception())


def spawn(coro, *, name: str | None = None) -> "asyncio.Task":
    """Spustí úlohu na pozadí, drží na ni referenci a zaloguje její pád."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_task_done)
    return task


# Výchozí sada kitů. Používá se, dokud není v ``kits`` žádný vlastní kit
# (``services.kit_catalog.get_kits``) – už se nečte z data/kits.json.
DEFAULT_KITS = [
    "AnchorPvP",
    "NetheriteSword",
    "IronAxe",
    "GoldSMP",
    "UHCMace",
    "RandomPot",
    "ShieldlessSMP",
]


async def kit_autocomplete(
    interaction: discord.Interaction, current: str
):
    """Autocomplete názvů kitů pro všechny commandy s parametrem `kit`.

    Katalog kitů je v PostgreSQL (``kits``), takže se čte odtud. Bez
    session_factory (nemáme DB) vrátíme výchozí sadu – žádný soubor se
    nečte a nehrozí, že by se do seznamu vrátilo něco jiného, než co má
    bot registrované.
    """
    from services.kit_catalog import get_kits as _get_kits_db

    session_factory = getattr(
        getattr(interaction, "client", None), "db_session_factory", None
    )
    try:
        kits = await _get_kits_db(session_factory=session_factory)
    except Exception:  # pragma: no cover - DB nedostupná, jen nabídka
        kits = list(DEFAULT_KITS)
    if current:
        kits = [k for k in kits if current.lower() in k.lower()]
    return [app_commands.Choice(name=k, value=k) for k in kits[:25]]


# Přidávání a odebírání kitů (``/addkit`` / ``/remkitit``) jede přes
# ``services.kit_catalog`` nad tabulkou ``kits`` – tady už nejsou, protože
# zapisovaly do ``data/kits.json``. Volající je cogs/kits.py.


def has_tester_role(member) -> bool:
    """Vrátí True, pokud je člen tester.

    Deleguje na ``services.permissions``: přesný allowlist ID rolí, když je
    nastaven (TESTER_ROLE_IDS), jinak původní shoda podle názvu role
    (TESTER_ROLE_FRAGMENT).
    """
    return _permissions_has_tester_role(member)


def today_cz() -> str:
    """Aktuální datum ve formátu DD.MM.YYYY (česky)."""
    return datetime.now().strftime("%d.%m.%Y")


def month_key() -> str:
    """Aktuální měsíc ve formátu MM.YYYY."""
    return datetime.now().strftime("%m.%Y")


def now_ms() -> int:
    """Aktuální čas v milisekundách (jako Date.now() v JS)."""
    return int(datetime.now().timestamp() * 1000)


# „LT3 + eval" už tu NENÍ. Evaly jsou řádky v tabulce ``evaluations``
# (player_id + kit_id, granted_by, revoked_at) přes ``services.evals``.
# Tyto funkce četly a zapisovaly ``data/evals.json`` a v produkci je nikdo
# nevolal – všichni volající už chodí do DB. Kdyby tu zůstaly, byl by to
# přesně ta druhá, neviditelný zdroj pravdy, která se tu snažíme odstranit:
# kód by vypadal funkční, ale jeho data by nikdo nečetl.
#
# ``data/evals.json`` po zásahu vypadá jako běžný export (viz
# ``services/player_export.py``), ne jako zdroj, ze kterého se čte.

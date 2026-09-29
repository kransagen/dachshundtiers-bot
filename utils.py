"""Drobné pomocné funkce sdílené napříč cogami."""

from datetime import datetime

import discord
from discord import app_commands

from services.permissions import has_tester_role as _permissions_has_tester_role
from storage import data_exists, load_data, save_data

# Výchozí sada kitů (použije se, dokud neexistuje data/kits.json)
DEFAULT_KITS = [
    "AnchorPvP",
    "NetheriteSword",
    "IronAxe",
    "GoldSMP",
    "UHCMace",
    "RandomPot",
    "ShieldlessSMP",
]


def get_kits(*, strict: bool = False):
    """Registrované kity z ``data/kits.json`` (nebo výchozí seznam).

    Názvy kitů se SJEDNOCUJÍ case-insensitive: case-duplicity v souboru
    („MolePVP" + „molepvp") se sloučí – zůstane první výskyt. Tím se
    sjednotí autocomplete, selecty i ``kit_display_map`` napříč cogami.

    ``strict=True`` používají ZAPISUJÍCÍ volající (:func:`add_kit`,
    :func:`remove_kit`): při poškozeném souboru se vyhodí
    :class:`~storage.DataCorruptionError` a zápis se vůbec neprovede. Bez
    toho by jim ``load_data`` vrátila výchozí seznam a writer by uložil
    default odvozený z nečitelného stavu, čímž by přepsal platná data.
    Čtenáři (autocomplete, panely, zobrazení) nechávají ``strict=False`` –
    jen zobrazují data, nic nezapisují, takže výchozí seznam je pro ně
    správná a užitečná fallback.
    """
    if not data_exists("kits.json"):
        return list(DEFAULT_KITS)
    raw = load_data("kits.json", [], strict=strict)
    seen = set()
    out = []
    for k in raw:
        if not isinstance(k, str) or not k.strip():
            continue
        key = k.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(k.strip())
    return out


def canonical_kit_name(kit: str) -> str:
    """Kanonický (display-case) název kitu z kits.json.

    Registrovaný kit (case-insensitive) → oficiální název („molepvp" →
    „MolePVP"). Nezaregistrovaný → původní vstup (našeptávání /result kity
    registruje samo). Používá se pro JEDNOTNÉ klíče v ``modes`` / ``history``.
    """
    kit = (kit or "").strip()
    if not kit:
        return kit
    target = kit.lower()
    for name in get_kits():
        if name.lower() == target:
            return name
    return kit


def migrate_mode_keys(mapping: dict, kit: str) -> str:
    """Kanonický klíč kitu v mapping (modes/history) + migrace case-klíče.

    Existující klíč s jiným case („MolePVP" vs „molepvp") se bezeztrátově
    přejmenuje na kanonický název kitu – NIKDY nevzniknou dva klíče jednoho
    kitu. Nový klíč se zapíše už jako kanonický. Vrací klíč k zápisu.
    """
    canonical = canonical_kit_name(kit)
    if not canonical:
        return ""
    for key in list(mapping.keys()):
        if str(key).strip().lower() == canonical.lower():
            if str(key) != canonical:
                mapping[canonical] = mapping.pop(key)
            return canonical
    return canonical


async def kit_autocomplete(
    interaction: discord.Interaction, current: str
):
    """Autocomplete názvů kitů pro všechny commandy s parametrem `kit`."""
    kits = get_kits() or list(DEFAULT_KITS)
    if current:
        kits = [k for k in kits if current.lower() in k.lower()]
    return [app_commands.Choice(name=k, value=k) for k in kits[:25]]


def add_kit(kit: str) -> bool:
    """Přidá kit (case-insensitive podle názvu). Vrátí False, pokud už existuje.

    Čte ``strict=True``: poškozený kits.json přeruší funkci výjimkou
    ``DataCorruptionError`` MÍSTO toho, aby se uložil výchozí seznam kiting
    a ostatní kity se tím ztratily.
    """
    kit = kit.strip()
    if not kit:
        return False
    kits = get_kits(strict=True)
    if any(existing.lower() == kit.lower() for existing in kits):
        return False
    kits.append(kit)
    save_data("kits.json", kits)
    return True


def remove_kit(kit: str) -> bool:
    """Odebere kit. Vrátí False, pokud v seznamu nebyl.

    Čte ``strict=True`` – stejně jako :func:`add_kit` nesmí poškozený
    kits.json vést k zápisu výchozího seznamu.
    """
    kit = kit.strip().lower()
    if not kit:
        return False
    kits = get_kits(strict=True)
    new_kits = [k for k in kits if k.lower() != kit]
    if len(new_kits) == len(kits):
        return False
    save_data("kits.json", new_kits)
    return True


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


# --- „LT3 + eval" ---------------------------------------------------------
# Status mezi LT3 a HT3: hráč má pořád roli LT3, ale může otevírat HT3+
# tickety. Uchovává se v ``data/evals.json`` (host-local, NIKDY necommitovat):
#
#     {"uhcmace": {"hrac_ign": 1769550000000, ...}, ...}
#
EVALS_FILE = "evals.json"


def _eval_ign(ign: str) -> str:
    return (ign or "").strip().lower()


def get_evals(*, strict: bool = False) -> dict:
    """Načte evalu.

    ``strict=True`` používají ZAPISUJÍCí volající (:func:`set_eval`,
    :func:`unset_eval`) – při nečitelném souboru vyhodí
    ``DataCorruptionError`` a zápis se neprovede. Bez toho by vrátila
    ``{}`` a writer by uložil prázdný dict, čímž by smazal všechny evaly.
    :func:`has_eval` je čistý čtenář a nechává ``strict=False``.
    """
    raw = load_data(EVALS_FILE, {}, strict=strict)
    return raw if isinstance(raw, dict) else {}


def eval_in(evals, ign: str, kit: str) -> bool:
    """Má hráč eval pro daný kit v už načteném dictu ``evals`` – bez I/O.

    Stejná normalizace jako :func:`has_eval`, ale nečte soubor, takže se
    dá zavolat uvnitř transakce ``services.store`` nad daty, která transakce
    už drží pod zámkem (viz ``services.tickets.create_ticket``, kde musí být
    rozhodnutí o eval shodné s tím, co později uloží).
    """
    if not isinstance(evals, dict):
        return False
    kit_key = (kit or "").strip().lower()
    bucket = evals.get(kit_key, {})
    return isinstance(bucket, dict) and _eval_ign(ign) in bucket


def has_eval(ign: str, kit: str) -> bool:
    """Má hráč (IGN) „LT3 + eval" pro daný kit?"""
    return eval_in(get_evals(), ign, kit)


def apply_eval_status(evals, ign: str, kit: str, now: int) -> bool:
    """Zapíše „LT3 + eval" do už načteného dictu ``evals`` – bez I/O.

    Stejná normalizace i tvar záznamu jako :func:`set_eval`; díky absenci
    čtení/zápisu souboru může zápis běžet uvnitř transakce ``services.store``.
    Při neplatném vstupu se dict nesahne a vrátí se ``False``.
    """
    kit_key = (kit or "").strip().lower()
    key = _eval_ign(ign)
    if not key or not kit_key or not isinstance(evals, dict):
        return False
    evals.setdefault(kit_key, {})[key] = now
    return True


def set_eval(ign: str, kit: str) -> bool:
    """Nastaví hráči eval pro kit. Vrátí False při neplatném vstupu.

    Čte ``strict=True`` (viz :func:`get_evals`) – poškozený evals.json nesmí
    vést k uložení prázdného dictu, který by smazal všechny existující
    evaly.
    """
    evals = get_evals(strict=True)
    if not apply_eval_status(evals, ign, kit, now_ms()):
        return False
    save_data(EVALS_FILE, evals)
    return True


def unset_eval(ign: str, kit: str) -> bool:
    """Odebere hráči eval pro kit. Vrátí False, pokud ho neměl.

    Čte ``strict=True`` (viz :func:`get_evals`) – jinak by poškozený soubor
    vedl k uložení prázdného dictu a ztrátě všech ostatních evalů.
    """
    kit_key = (kit or "").strip().lower()
    evals = get_evals(strict=True)
    bucket = evals.get(kit_key, {})
    if not isinstance(bucket, dict) or _eval_ign(ign) not in bucket:
        return False
    del bucket[_eval_ign(ign)]
    if not bucket:
        del evals[kit_key]
    save_data(EVALS_FILE, evals)
    return True

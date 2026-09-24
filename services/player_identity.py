"""Identita hráče – Discord ID je PRIMÁRNÍ identita (bez discord.py).

Kanonická databáze hráčů (data/players.json) je seznam záznamů:
``username`` = aktuální IGN, ``discordId`` = Discord ID hráče (stabilní),
``modes`` = tiery per kit, ``history`` = historie tierů. Tento modul je
jediné místo, kde se identita hráče řeší:

  - ``discordId`` je stabilní primární klíč – IGN se může měnit,
  - ``username`` (IGN) je zobrazované jméno – změna IGN přejmenuje záznam,
  - záznamy BEZ ``discordId`` jsou historické („neobsazené"): připojení
    Discord ID (= adopce) zachovává historii a NIKDY neslučuje dva hráče,
  - IGN patřící ZÁZNAMU S JINÝM Discord ID → konflikt (PlayerIdentityConflict),
    operace se odmítá – nikdy se nehádá a nikdy neslučuje automaticky.

Stav se nikdy nemutuje na místě – funkce pracují s kopiemi (čisté funkce,
stejný styl jako apply_result_to_players v services/results.py).
"""

# Výsledek claim_ign.
CLAIM_CREATED = "created"
CLAIM_RENAMED = "renamed"
CLAIM_ADOPTED = "adopted"
CLAIM_UNCHANGED = "unchanged"
CLAIM_CONFLICT = "conflict"

# Zdroj nalezení hráče (resolve_player).
RESOLVE_DISCORD_ID = "discord_id"
RESOLVE_IGN = "ign"


class PlayerIdentityConflict(ValueError):
    """Zadané IGN patří jinému hráči (jinému Discord ID) – operaci odmítnout."""


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _copy_players(players) -> list:
    return [
        {
            **p,
            "modes": dict(p.get("modes") or {}),
            "history": {k: list(v) for k, v in (p.get("history") or {}).items()},
        }
        for p in (players or [])
        if isinstance(p, dict)
    ]


def find_by_discord_id(players, discord_id):
    """Hráč podle Discord ID (stabilní primární klíč), nebo None."""
    did = str(discord_id) if discord_id else ""
    if not did:
        return None
    for p in players or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("discordId") or "") == did:
            return p
    return None


def find_by_ign(players, ign: str):
    """Hráč podle IGN (case-insensitive), nebo None."""
    name = _norm(ign)
    if not name:
        return None
    for p in players or []:
        if not isinstance(p, dict):
            continue
        if _norm(p.get("username") or "") == name:
            return p
    return None


def resolve_player(players, *, discord_id=None, ign=None):
    """Najde hráče: přednost má Discord ID, pak IGN.

    Vrací ``(player, zdroj)``, kde zdroj je RESOLVE_DISCORD_ID / RESOLVE_IGN,
    nebo ``(None, "")``, když hráč neexistuje.
    """
    if discord_id:
        player = find_by_discord_id(players, discord_id)
        if player is not None:
            return player, RESOLVE_DISCORD_ID
    if ign:
        player = find_by_ign(players, ign)
        if player is not None:
            return player, RESOLVE_IGN
    return None, ""


def claim_ign(players, *, discord_id=None, ign: str) -> tuple:
    """Přiřadí IGN k hráči s ohledem na stabilní Discord ID.

    Pravidla (zadání: „Discord ID je primární identita"):
      - hráč nalezený podle Discord ID a IGN sedí          -> neutrální stav
      - stejný hráč, jiné IGN (nikomu jinému nepatří)      -> přejmenování
      - záznam bez Discord ID odpovídá zadanému IGN        -> adopce
        (připojí Discord ID, zachová historii; neslučuje hráče)
      - IGN patří záznamu s JINÝM Discord ID               -> konflikt
      - bez Discord ID: klasická IGN shoda (legacy chování)

    Vrací ``(players, player, outcome)``. Při konfliktu zvedá
    ``PlayerIdentityConflict`` – volající rozhodne, jak operaci odmítnout.
    """
    players = _copy_players(players)
    ign_clean = (ign or "").strip()
    if not ign_clean:
        raise PlayerIdentityConflict("IGN je prázdné – nelze přiřadit identitu.")

    if discord_id:
        did = str(discord_id)
        by_did = find_by_discord_id(players, did)
        if by_did is not None:
            if _norm(by_did.get("username") or "") == _norm(ign_clean):
                return players, by_did, CLAIM_UNCHANGED
            other = find_by_ign(players, ign_clean)
            if other is not None and other is not by_did:
                raise PlayerIdentityConflict(
                    f"IGN `{ign_clean}` patří jinému hráči (Discord ID "
                    f"`{other.get('discordId') or '?'}`) – sloučení se odmítá."
                )
            by_did["username"] = ign_clean
            return players, by_did, CLAIM_RENAMED

        by_ign = find_by_ign(players, ign_clean)
        if by_ign is None:
            player = {
                "username": ign_clean,
                "discordId": did,
                "modes": {},
                "history": {},
            }
            players.append(player)
            return players, player, CLAIM_CREATED
        if by_ign.get("discordId"):
            raise PlayerIdentityConflict(
                f"IGN `{ign_clean}` patří jinému hráči (Discord ID "
                f"`{by_ign['discordId']}`) – operace se odmítá."
            )
        by_ign["discordId"] = did
        return players, by_ign, CLAIM_ADOPTED

    # Legacy: bez Discord ID – čistá IGN shoda (původní chování).
    by_ign = find_by_ign(players, ign_clean)
    if by_ign is None:
        players.append({"username": ign_clean, "modes": {}, "history": {}})
        return players, players[-1], CLAIM_CREATED
    return players, by_ign, CLAIM_UNCHANGED
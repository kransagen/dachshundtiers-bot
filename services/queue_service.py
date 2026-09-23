"""Čistá logika front + transakční operace nad JSON stavem (bez discord.py).

Veškeré změny ``queue.json`` a příbuzných souborů probíhají atomicky
(čtení + kontrola + zápis v jednom kritickém úseku, viz ``services.store``),
takže souběžné interakce nemůžou:
- duplicitně zapsat hráče do fronty,
- obejít cooldown,
- ztratit zápis (join vs. pull vs. leave), a
- vytažení (pull) hráče už není „dvakrát".
"""

from services.store import transaction


def cooldown_remaining(cooldowns, user_id: str, now: int, cooldown_ms: int):
    """Zbývající milisekundy cooldownu hráče, nebo ``None``, když už vypršel."""
    if not isinstance(cooldowns, dict):
        return None
    last = cooldowns.get(user_id)
    if last is None:
        return None
    remaining = cooldown_ms - (now - last)
    return remaining if remaining > 0 else None


def already_in_queue(queue, user_id: str, kit_key: str) -> bool:
    """Je hráč (podle ID) už zapsaný ve frontě daného kitu?"""
    kit_key = str(kit_key).lower()
    uid = str(user_id)
    return any(
        p.get("id") == uid and str(p.get("kit", "")).lower() == kit_key
        for p in queue
    )


def make_entry(user_id: str, username: str, ign: str, kit: str, joined_at_ms: int) -> dict:
    """Nový záznam hráče ve frontě (stejný tvar jako dřív)."""
    return {
        "id": str(user_id),
        "username": username,
        "ign": ign,
        "kit": kit,
        "joinedAt": joined_at_ms,
        "testerId": None,
    }


async def join_queue(
    user_id: str,
    username: str,
    ign: str,
    kit: str,
    *,
    joined_at_ms: int,
    cooldown_ms: int,
) -> dict:
    """Transakčně přidá hráče do fronty (aktivní fronta + cooldown + duplicita).

    Vrací slovník s klíčem ``result``:
      - ``"joined"``    → hráč byl přidán,
      - ``"closed"``    → fronta už není aktivní,
      - ``"cooldown"``  → cooldown stále běží (klíč ``remaining`` = zbývající ms),
      - ``"duplicate"`` → hráč už ve frontě kitu je.
    """
    kit_key = str(kit).lower()
    uid = str(user_id)
    now = joined_at_ms

    async def _run(tx):
        active_queues = tx.get("active_queues.json", {})
        if not active_queues.get(kit_key):
            return {"result": "closed"}

        cooldowns = tx.get("cooldowns.json", {})
        remaining = cooldown_remaining(cooldowns, uid, now, cooldown_ms)
        if remaining is not None:
            return {"result": "cooldown", "remaining": remaining}

        queue = tx.get("queue.json")
        if already_in_queue(queue, uid, kit_key):
            return {"result": "duplicate"}

        queue.append(make_entry(uid, username, ign, kit, now))
        tx.set("queue.json", queue)
        return {"result": "joined"}

    return await transaction(
        ("active_queues.json", "cooldowns.json", "queue.json"), _run
    )


async def leave_queue(user_id: str, kit_key: str) -> bool:
    """Transakčně vyjme hráče z fronty daného kitu. Vrací True, když byl odebrán."""
    kit_key = str(kit_key).lower()
    uid = str(user_id)

    async def _run(tx):
        queue = tx.get("queue.json")
        new_queue = [
            p
            for p in queue
            if not (p.get("id") == uid and str(p.get("kit", "")).lower() == kit_key)
        ]
        if len(new_queue) == len(queue):
            return False
        tx.set("queue.json", new_queue)
        return True

    return await transaction(("queue.json",), _run)


async def pop_for_kit(kit_key: str):
    """Transakčně odebere PRVNÍHO hráče kitu z fronty (pull).

    Vrací záznam hráče, nebo ``None``, když fronta kitu už nikoho nemá.
    """
    kit_key = str(kit_key).lower()

    async def _run(tx):
        queue = tx.get("queue.json")
        index = next(
            (
                i
                for i, p in enumerate(queue)
                if str(p.get("kit", "")).lower() == kit_key
            ),
            None,
        )
        if index is None:
            return None
        player = queue.pop(index)
        tx.set("queue.json", queue)
        return player

    return await transaction(("queue.json",), _run)


async def remove_by_player_id(player_id: str) -> bool:
    """Transakčně vyjme hráče z fronty podle Discord ID (nezávisle na kitu).

    Vrací True, když byl ve frontě nalezen a odebrán.
    """
    uid = str(player_id)

    async def _run(tx):
        queue = tx.get("queue.json")
        new_queue = [p for p in queue if p.get("id") != uid]
        if len(new_queue) == len(queue):
            return False
        tx.set("queue.json", new_queue)
        return True

    return await transaction(("queue.json",), _run)


async def save_pulled_player(player: dict, channel_id: int) -> None:
    """Zapíše/aktualizuje záznam vytaženého hráče do ``pulled_players.json``.

    Jeden záznam na hráče (přepis je záměrný – hráč je vždy „vytažený" jen do
    jedné roomky; dvojitý pull už nepustí atomický pop výše).
    """

    async def _run(tx):
        pulled = tx.get("pulled_players.json", {})
        pulled[str(player.get("id", ""))] = {
            "channel": str(channel_id),
            "player": {
                "id": str(player.get("id", "")),
                "username": player.get("username", ""),
                "ign": player.get("ign", ""),
                "kit": str(player.get("kit", "")),
                "joinedAt": player.get("joinedAt", 0),
            },
        }
        tx.set("pulled_players.json", pulled)

    return await transaction(("pulled_players.json",), _run)


async def remove_pulled_player(player_id: str) -> bool:
    """Smaže záznam vytaženého hráče. Vrací True, když existoval."""
    uid = str(player_id)

    async def _run(tx):
        pulled = tx.get("pulled_players.json", {})
        if uid not in pulled:
            return False
        del pulled[uid]
        tx.set("pulled_players.json", pulled)
        return True

    return await transaction(("pulled_players.json",), _run)
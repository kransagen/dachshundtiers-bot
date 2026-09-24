"""Sdílené orchestrace helpery cogy (discord.py vrstva, bez obchodní logiky).

Odstraňuje duplicitu napříč cogy:

- ``guild_members`` / ``member_to_dict`` – členové serveru → normalizované dict
  (dříve 3×: playersync, checkweb, edituser),
- ``kit_display_map`` – display-case názvy kitů pro case-insensitive klíče
  kit_roles.json / modes (dříve 3×),
- ``apply_role_actions`` – aplikace plánu rolí (add/remove) s per-akce
  vyhodnocením – sdíleno /sync discord a /edituser,
- ``admin_gate_error`` – guild + admin kontrola pro příkazy i view tlačítka,
- ``save_players`` – atomický zápis ``players.json`` (per-záznamová rozhodnutí).

Business logika zůstává ve službách (services/role_sync, services/store, ...);
tohle je jen sdílený „Discord glue“.
"""

import logging

import discord

from services.permissions import has_admin_role
from services.role_sync import make_member
from services.store import transaction
from utils import get_kits

log = logging.getLogger("dachshundtiers")


async def guild_members(guild: discord.Guild) -> list:
    """Všichni členové serveru bez botů (plný seznam, jinak cache)."""
    members = [m for m in guild.members if not m.bot]
    try:
        fetched = await guild.fetch_members().flatten()
        if fetched:
            members = [m for m in fetched if not m.bot]
    except Exception:  # noqa: BLE001 – bez members intentu fallback na cache
        pass
    return members


def member_to_dict(member) -> dict:
    """Normalizovaný člen (id, jména, role_ids) pro role analýzy."""
    return make_member(
        member.id,
        member.display_name,
        {str(r.id) for r in member.roles},
        extra_names=[member.name, member.nick],
    )


def kit_display_map() -> dict:
    """Display-case názvy kitů: lowercase klíč → oficiální název."""
    return {str(k).lower(): str(k) for k in get_kits()}


async def apply_role_actions(guild: discord.Guild, actions: list) -> list:
    """Aplikuje akce (add/remove rolí); každá akce se vyhodnotí zvlášť.

    Chyby se nikdy nešíří dál – každá akce skončí s ``ok`` / ``error``.
    """
    applied = []
    for action in actions:
        member_id = str(action.get("member_id") or "")
        role_id = str(action.get("role_id") or "")
        record = {
            "op": action.get("op"),
            "memberId": member_id,
            "memberName": action.get("member_name") or "",
            "roleId": role_id,
            "kit": action.get("kit") or "",
            "tier": action.get("tier") or "",
            "ok": False,
            "error": None,
        }
        member = None
        if member_id.isdigit():
            member = guild.get_member(int(member_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(member_id))
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    member = None
        role = guild.get_role(int(role_id)) if role_id.isdigit() else None
        if member is None:
            record["error"] = "člen není na serveru"
        elif role is None:
            record["error"] = "role neexistuje"
        else:
            try:
                if action.get("op") == "add":
                    await member.add_roles(role)
                else:
                    await member.remove_roles(role)
                record["ok"] = True
            except (discord.Forbidden, discord.HTTPException) as err:
                record["error"] = str(err)
        applied.append(record)
    return applied


def admin_gate_error(interaction) -> str | None:
    """Vrátí hlášku, když interakce neprošla gate (guild + admin), jinak None."""
    if interaction.guild is None:
        return "❌ Pouze na serveru."
    if not has_admin_role(interaction.user):
        return "❌ Pouze pro administrátory."
    return None


async def save_players(players: list) -> None:
    """Atomický zápis players.json (transakce; korupce → DataCorruptionError)."""

    async def _run(tx):
        tx.set("players.json", players)

    await transaction(("players.json",), _run)
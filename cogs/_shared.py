"""Sdílené orchestrace helpery cogy (discord.py vrstva, bez obchodní logiky).

Odstraňuje duplicitu napříč cogy:

- ``guild_members`` / ``member_to_dict`` – členové serveru → normalizované dict
  (dříve 3×: playersync, checkweb, edituser),
- ``kit_display_map`` – display-case názvy kitů pro case-insensitive klíče
  kit_roles.json / modes (dříve 3×),
- ``apply_role_actions`` – aplikace plánu rolí (add/remove) s per-akce
  vyhodnocením – sdíleno /sync discord a /edituser,
- ``admin_gate_error`` – guild + admin kontrola pro příkazy i view tlačítka.
  (Bývalý ``save_players`` – atomický zápis ``players.json`` – je pryč:
  jediný zapisovač players.json je ``services.player_export``, a to jen
  jako export z PostgreSQL.)

Business logika zůstává ve službách (services/role_sync, services/store, ...);
tohle je jen sdílený „Discord glue“.
"""

import logging

import discord

from services.kit_catalog import get_kits
from services.permissions import has_admin_role
from services.role_sync import make_member

log = logging.getLogger("dachshundtiers")


async def guild_members(guild: discord.Guild) -> list:
    """Všichni členové serveru bez botů (plný seznam, jinak cache)."""
    members = [m for m in guild.members if not m.bot]
    try:
        fetched = await guild.fetch_members().flatten()
        if fetched:
            members = [m for m in fetched if not m.bot]
    except Exception as err:  # noqa: BLE001 – bez members intentu fallback na cache
        log.warning(
            "fetch_members() selhalo (%s: %s) – používám cache (%d členů). "
            "Výsledek analýzy může být neúplný.",
            type(err).__name__,
            err,
            len(members),
        )
    return members


def member_to_dict(member) -> dict:
    """Normalizovaný člen (id, jména, role_ids) pro role analýzy."""
    return make_member(
        member.id,
        member.display_name,
        {str(r.id) for r in member.roles},
        extra_names=[member.name, member.nick],
    )


async def kit_display_map(session_factory=None) -> dict:
    """Display-case názvy kitů: lowercase klíč → oficiální název."""
    kits = await get_kits(session_factory=session_factory)
    return {str(k).lower(): str(k) for k in kits}


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


async def _rollback_conflicts_with_later_promotion(
    session_factory, *, discord_id: int, kit_key: str, target_ts: int
) -> str | None:
    """H3 audit fix: has this member been promoted via ``/result``/
    ``/topresult`` for this kit AFTER the sync being rolled back?

    ``/sync discord-rollback`` only replays ``playersync_log.json`` (scoped
    to ``/sync discord apply``) — a later, unrelated ``auto_grant_kit_role``
    promotion is never logged there at all, so rollback's own audit trail
    has no way to know about it. Reintroducing the old, pre-sync role in
    that case would silently give the member back a stale tier role
    alongside their newer, correct one (the exact "multiple tier roles for
    one kit" conflict ``/sync check`` treats as a serious anomaly) — via the
    one tool whose entire purpose is safe, audited reversal.

    Returns a human-readable reason string when a later promotion exists
    (the caller must then SKIP this action, not apply it), or ``None`` when
    it's safe to proceed. Returns ``None`` (no guard) when PostgreSQL isn't
    configured — this check is a DB-mirror-only safety net.
    """
    if session_factory is None:
        return None
    from datetime import datetime, timezone

    from db.repositories.kits import KitRepository
    from db.repositories.players import PlayerRepository
    from db.repositories.tiers import TierHistoryRepository
    from db.services.session import transaction as db_transaction

    target_dt = datetime.fromtimestamp(target_ts / 1000, tz=timezone.utc)
    async with db_transaction(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, int(discord_id))
        if player is None:
            return None
        kit = await KitRepository().get_by_key(session, (kit_key or "").strip().lower())
        if kit is None:
            return None
        history = await TierHistoryRepository().list_for_player(
            session, player_id=player.id, kit_id=kit.id, limit=20
        )
    for h in history:
        if h.source == "promotion" and h.changed_at > target_dt:
            return (
                f"přeskočeno – hráč byl po cíleném syncu ({target_dt.isoformat()}) "
                f"znovu povýšen přes /result ({h.changed_at.isoformat()}); "
                "návrat staré role by ji nechal vedle nové"
            )
    return None


async def apply_rollback_actions(
    guild: discord.Guild,
    actions: list,
    *,
    session_factory=None,
    target_ts: int | None = None,
) -> list:
    """Aplikuje rollback akce (inverze provedeného syncu); každá zvlášť.

    Oproti ``apply_role_actions`` navíc rozlišuje idempotentní stavy
    (``already_correct``), takže rollback se dá bezpečně spustit dvakrát:

    - op ``add`` a role už je přítomná  → ``already_correct``, bez volání API,
    - op ``remove`` a role nepřítomná   → ``already_correct``, bez volání API,
    - jinak provede zásah (``applied``) / selže (``failed`` + error).

    H3 audit fix: pokud (``session_factory``/``target_ts`` dodané) hráč byl
    po cíleném syncu znovu povýšen přes ``/result`` pro stejný kit, akce se
    PŘESKOČÍ (``skipped_newer_promotion``) místo aplikace – viz
    ``_rollback_conflicts_with_later_promotion``.

    Identita je výhradně přes ``member_id`` / ``role_id`` z auditního logu.
    Chyby (člen/role nenalezen, Forbidden, HTTP) se nikdy nešíří dál.
    """
    results = []
    for action in actions:
        member_id = str(action.get("member_id") or "")
        role_id = str(action.get("role_id") or "")
        op = action.get("op")
        record = {
            "op": op,
            "original_op": action.get("original_op") or "",
            "memberId": member_id,
            "memberName": action.get("member_name") or "",
            "roleId": role_id,
            "kit": action.get("kit") or "",
            "tier": action.get("tier") or "",
            "status": "failed",  # nezdar je implicitní – vždy se přepíše
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
        elif (
            session_factory is not None
            and target_ts is not None
            and member_id.isdigit()
            and (
                conflict := await _rollback_conflicts_with_later_promotion(
                    session_factory,
                    discord_id=int(member_id),
                    kit_key=action.get("kit") or "",
                    target_ts=target_ts,
                )
            )
        ):
            record["status"] = "skipped_newer_promotion"
            record["error"] = conflict
        else:
            held = {str(r.id) for r in (getattr(member, "roles", None) or [])}
            if op == "add" and role_id in held:
                record["status"] = "already_correct"
            elif op == "remove" and role_id not in held:
                record["status"] = "already_correct"
            else:
                try:
                    if op == "add":
                        await member.add_roles(role)
                    else:
                        await member.remove_roles(role)
                    record["status"] = "applied"
                except (discord.Forbidden, discord.HTTPException) as err:
                    record["error"] = str(err)
        results.append(record)
    return results


def admin_gate_error(interaction) -> str | None:
    """Vrátí hlášku, když interakce neprošla gate (guild + admin), jinak None."""
    if interaction.guild is None:
        return "❌ Pouze na serveru."
    if not has_admin_role(interaction.user):
        return "❌ Pouze pro administrátory."
    return None
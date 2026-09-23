"""Správa automatických rolí kitů a tierů po `/result`.

Mapování žije v ``data/kit_roles.json`` (host-local, NIKDY necommitovat –
obsahuje serverové ID rolí)::

    {
      "molepvp": {"S": "123456789012345678", "A": "123456789012345679"},
      "uhcmace": {"S": "111111111111111111"}
    }

Krátký slovník:
- /setkitrole  – namapuje roli tieru pro kit,
- /unsetkitrole– zruší mapování,
- /kitrole     – vypíše všechna mapování,
- /checkweb    – zkontroluje hráče u každého kitu a chybějící/změněné tiery
                 zapíše do players.json (web) a synchronizuje na GitHub.

Po `/result` se hráči automaticky dá role nového tieru a odeberou se ostatní
tier role stejného kitu.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import github_sync
from services.store import transaction
from storage import load_data, save_data
from utils import get_kits, has_admin_role, has_tester_role, kit_autocomplete, today_cz

log = logging.getLogger("dachshundtiers")

KIT_ROLES_FILE = "kit_roles.json"

# ---------------------------------------------------------------------------
# Pomocné funkce pro /checkweb (zápis tierů na web / players.json + GitHub)
# ---------------------------------------------------------------------------
def _match_web_player(players: list, member) -> dict | None:
    """Najde hráče v players.json podle nicku / display name / username."""
    candidates = {member.nick, member.display_name, member.name}
    names = {c.strip().lower() for c in candidates if c and c.strip()}
    return next(
        (
            p
            for p in players
            if str(p.get("username", "")).strip().lower() in names
        ),
        None,
    )


def _find_mode_key(modes: dict, kit_key_lower: str) -> str | None:
    """Klíč módu bez ohledu na velikost písmen („MolePVP" vs „molepvp")."""
    for key in modes:
        if str(key).lower() == kit_key_lower:
            return key
    return None


async def auto_grant_kit_role(
    guild: discord.Guild,
    member_id: str,
    kit_key: str,
    tier_up: str,
) -> str:
    """Automaticky dá hráči roli tieru kitu a odebere ostatní tiery kitu.

    Vrací krátkou poznámku pro potvrzovací zprávu `/result` (``""`` = nic).
    Zápis výsledku nikdy nenaruší – všechny chyby se jen zalogují.
    """
    roles_map = load_data(KIT_ROLES_FILE, {})
    kit_map = roles_map.get(kit_key)
    if not kit_map:
        return ""

    role_id = kit_map.get(tier_up)
    if role_id is None:
        return (
            f"\n💡 Pro kit **{kit_key}** nemáš namapovanou roli tieru **{tier_up}** "
            f"– nastav ji přes `/setkitrole kit:{kit_key} tier:{tier_up} role:@Role`."
        )

    member = guild.get_member(int(member_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(member_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    if member is None:
        return (
            f"\n⚠️ Role **<@&{role_id}>** se nedá dát – hráč <@{member_id}> není na serveru."
        )

    role_obj = guild.get_role(int(role_id))
    if role_obj is None:
        log.warning("Role %s pro kit %s tier %s neexistuje", role_id, kit_key, tier_up)
        return (
            f"\n⚠️ Role <@&{role_id}> se nenašla – smaž ji a namapuj znovu přes "
            "`/setkitrole`."
        )

    try:
        await member.add_roles(role_obj)

        # Odebrání ostatních tier rolí stejného kitu (hráč má vždy jen jeden tier)
        other_roles = [
            guild.get_role(int(rid))
            for tier, rid in kit_map.items()
            if tier != tier_up
        ]
        to_remove = [r for r in other_roles if r is not None and r in member.roles]
        if to_remove:
            await member.remove_roles(*to_remove)

        return f"\n🎖️ Hráči byla dána role **{role_obj.mention}**."
    except (discord.Forbidden, discord.HTTPException) as err:
        log.warning("Nelze udělit roli %s pro %s: %s", role_id, member_id, err)
        return (
            f"\n⚠️ Roli <@&{role_id}> se nepovedlo udělit – zkontroluj oprávnění "
            "bota (Manage Roles a hierarchii rolí)."
        )


class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /setkitrole
    # ------------------------------------------------------------------
    @app_commands.command(
        name="setkitrole",
        description="Namapuje roli tieru pro kit (rozdává se po /result)",
    )
    @app_commands.describe(
        kit="Název kitu (např. MolePVP)",
        tier="Tier (např. S, A, B)",
        role="Role, kterou hráč dostane za tento tier",
    )
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def setkitrole(
        self,
        interaction: discord.Interaction,
        kit: str,
        tier: str,
        role: discord.Role,
    ) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        kit_name = kit.strip()
        kit_key = kit_name.lower()
        tier_up = tier.strip().upper()
        if not kit_key or not tier_up:
            return await interaction.response.send_message(
                "❌ Zadej platný název kitu a tieru.", ephemeral=True
            )

        async def _run(tx):
            roles_map = tx.get(KIT_ROLES_FILE, {})
            kit_map = roles_map.setdefault(kit_key, {})
            kit_map[tier_up] = str(role.id)
            tx.set(KIT_ROLES_FILE, roles_map)

        await transaction((KIT_ROLES_FILE,), _run)

        await interaction.response.send_message(
            f"✅ Role **{role.mention}** je namapovaná na kit **{kit_name}** — "
            f"tier **{tier_up}**. Po `/result` ji hráč dostane automaticky."
        )

    # ------------------------------------------------------------------
    # /unsetkitrole
    # ------------------------------------------------------------------
    @app_commands.command(
        name="unsetkitrole",
        description="Zruší mapování role tieru pro kit",
    )
    @app_commands.describe(
        kit="Název kitu (např. MolePVP)",
        tier="Tier (např. S, A, B)",
    )
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def unsetkitrole(
        self,
        interaction: discord.Interaction,
        kit: str,
        tier: str,
    ) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        kit_key = kit.strip().lower()
        tier_up = tier.strip().upper()

        async def _run(tx):
            roles_map = tx.get(KIT_ROLES_FILE, {})
            kit_map = roles_map.get(kit_key, {})
            if tier_up not in kit_map:
                return False
            del kit_map[tier_up]
            if not kit_map:
                roles_map.pop(kit_key, None)
            tx.set(KIT_ROLES_FILE, roles_map)
            return True

        removed = await transaction((KIT_ROLES_FILE,), _run)
        if not removed:
            return await interaction.response.send_message(
                f"❌ Pro kit **{kit.strip()}** a tier **{tier_up}** žádné "
                "mapování není.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"🗑️ Mapování role pro kit **{kit.strip()}** (tier **{tier_up}**) "
            "bylo zrušeno."
        )

    # ------------------------------------------------------------------
    # /kitrole
    # ------------------------------------------------------------------
    @app_commands.command(
        name="kitrole",
        description="Vypíše namapované role kitů a tierů",
    )
    async def kitrole(self, interaction: discord.Interaction) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro testery.", ephemeral=True
            )

        roles_map = load_data(KIT_ROLES_FILE, {})
        if not roles_map:
            return await interaction.response.send_message(
                "ℹ️ Žádné role zatím nejsou namapované. Použij `/setkitrole`.",
                ephemeral=True,
            )

        lines = []
        for kit_key in sorted(roles_map):
            kit_map = roles_map[kit_key]
            parts = ", ".join(
                f"**{tier}** → <@&{rid}>" for tier, rid in sorted(kit_map.items())
            )
            lines.append(f"• **{kit_key.capitalize()}**: {parts}")

        embed = discord.Embed(
            title="🎖️ Mapování rolí (kit → tier)",
            description="\n".join(lines),
            color=0xF59E0B,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /checkweb – projede hráče u každého kitu a chybějící tiery zapíše
    #             do players.json + synchronizuje na GitHub (web)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="checkweb",
        description="Zkontroluje hráče u každého kitu a chybějící tiery zapíše na web",
    )
    async def checkweb(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro testery.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        roles_map = load_data(KIT_ROLES_FILE, {})
        if not roles_map:
            return await interaction.followup.send(
                "ℹ️ Žádné mapování rolí – nejdřív nastav `/setkitrole`.",
                ephemeral=True,
            )

        # 1) Displejové názvy kitů (data/kits.json) pro zápis do players.json.
        kit_display = {str(k).lower(): str(k) for k in get_kits()}

        # 2) Členové serveru (zkusíme plný seznam, jinak cache)
        members = [m for m in guild.members if not m.bot]
        try:
            fetched = await guild.fetch_members().flatten()
            if fetched:
                members = [m for m in fetched if not m.bot]
        except Exception:  # noqa: BLE001 – bez members intentu fallback na cache
            pass

        # 3) Aktuální webová data: nejlépe přímo z GitHubu (zdroj pravdy),
        #    jinak lokální kopie players.json.
        players, _, _ = await github_sync.fetch_players()
        source = "GitHub"
        if players is None:
            players = load_data("players.json") or []
            source = "lokální kopie"

        # 4) Merge funkce: pro každý kit projde členy s tier rolí a chybějící /
        #    změněné tiery zapíše do seznamu hráčů (modes + history s dnešním
        #    datem). Volá se na webovém snapshotu a znovu po každém konfliktu
        #    (409) při push – proto je čistá a počítadla drží ve merge_state.
        today = today_cz()
        merge_state: dict = {}

        def _merge(fresh_players: list) -> list:
            merge_state.clear()
            out = [dict(p) for p in (fresh_players or [])]
            summary_local = []
            changes_local = []
            checked = 0
            added_total = 0
            updated_total = 0
            ok_total = 0

            for kit_key, tier_map in roles_map.items():
                kit_key = (kit_key or "").strip().lower()
                if not kit_key or not isinstance(tier_map, dict) or not tier_map:
                    continue
                kit_name = kit_display.get(kit_key, kit_key.capitalize())

                kit_roles = []
                for tier, role_id in tier_map.items():
                    rid = str(role_id)
                    if not rid.isdigit():
                        continue
                    role_obj = guild.get_role(int(rid))
                    if role_obj is not None:
                        kit_roles.append((role_obj, str(tier).strip().upper() or str(tier)))
                if not kit_roles:
                    continue

                added = updated = ok = 0
                for member in members:
                    held = [
                        tier for role_obj, tier in kit_roles if role_obj in member.roles
                    ]
                    if not held:
                        continue
                    checked += 1

                    player = _match_web_player(out, member)
                    if player is None:
                        username = (member.display_name or member.name).strip() or member.name
                        out.append(
                            {
                                "username": username,
                                "modes": {kit_name: held[0]},
                                "history": {
                                    kit_name: [{"date": today, "tier": held[0]}]
                                },
                            }
                        )
                        added += 1
                        changes_local.append(
                            f"➕ **{kit_name}** – {username} (**{held[0]}**)"
                        )
                        continue

                    player.setdefault("modes", {})
                    player.setdefault("history", {})
                    mode_key = _find_mode_key(player["modes"], kit_key) or kit_name
                    existing = player["modes"].get(mode_key)
                    if existing and str(existing).strip().upper() == held[0]:
                        ok += 1
                        continue

                    player["modes"][mode_key] = held[0]
                    player["history"].setdefault(mode_key, [])
                    player["history"][mode_key].append({"date": today, "tier": held[0]})
                    if existing:
                        updated += 1
                        changes_local.append(
                            f"✏️ **{kit_name}** – {player.get('username', member.display_name)}: "
                            f"{existing} → **{held[0]}**"
                        )
                    else:
                        added += 1
                        changes_local.append(
                            f"➕ **{kit_name}** – {player.get('username', member.display_name)} "
                            f"(**{held[0]}**)"
                        )

                added_total += added
                updated_total += updated
                ok_total += ok
                summary_local.append(
                    f"• **{kit_name}**: ✅ {ok} · ➕ {added} · ✏️ {updated}"
                )

            merge_state.update(
                summary=summary_local,
                changes=changes_local,
                total_checked=checked,
                total_added=added_total,
                total_updated=updated_total,
                total_ok=ok_total,
            )
            return out

        # 5) Sloučení na výchozím snapshotu + lokální uložení + push na GitHub.
        #    Při konfliktu (409) se merge znovu aplikuje na čerstvá webová data
        #    a PUT se zopakuje (vše serializované, viz github_sync.py).
        merged = _merge(players)
        save_data("players.json", merged)
        gh_ok, gh_msg, last_built = await github_sync.push_players(
            "checkweb: synchronizace tieru na web",
            _merge,
            success_message="✅ players.json synchronizováno na GitHub – web je aktuální.",
        )
        if gh_ok and last_built is not None:
            save_data("players.json", last_built)

        # Počítadla a změny z posledního běhu merge (odpovídají tomu, co je
        # teď na webu / v lokálním souboru).
        summary = merge_state["summary"]
        changes = merge_state["changes"]
        total_checked = merge_state["total_checked"]
        total_added = merge_state["total_added"]
        total_updated = merge_state["total_updated"]
        total_ok = merge_state["total_ok"]

        embed = discord.Embed(
            title="🔎 /checkweb – synchronizace tierů na web",
            description=(
                f"Zkontrolováno hráčů (kit × hráč): **{total_checked}**\n"
                f"✅ Již zapsáno: **{total_ok}**\n"
                f"➕ Nově zapsáno: **{total_added}**\n"
                f"✏️ Aktualizováno: **{total_updated}**\n"
                f"📦 Zdroj dat: {source}\n\n"
                + "\n".join(summary)
            ),
            color=0x10B981 if total_added + total_updated == 0 else 0xF59E0B,
        )
        if changes:
            lines = changes[:15]
            if len(changes) > 15:
                lines.append(f"…a dalších {len(changes) - 15} změn")
            embed.add_field(
                name="Změny na webu",
                value="\n".join(lines),
                inline=False,
            )
        embed.set_footer(text=f"{gh_msg} · {today}")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
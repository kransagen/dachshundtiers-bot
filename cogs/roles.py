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
- /kitrole     – vypíše všechna mapování.

Po `/result` se hráči automaticky dá role nového tieru a odeberou se ostatní
tier role stejného kitu.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from storage import load_data, save_data
from utils import has_tester_role

log = logging.getLogger("dachshundtiers")

KIT_ROLES_FILE = "kit_roles.json"


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
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        kit="Název kitu (např. MolePVP)",
        tier="Tier (např. S, A, B)",
        role="Role, kterou hráč dostane za tento tier",
    )
    async def setkitrole(
        self,
        interaction: discord.Interaction,
        kit: str,
        tier: str,
        role: discord.Role,
    ) -> None:
        kit_name = kit.strip()
        kit_key = kit_name.lower()
        tier_up = tier.strip().upper()
        if not kit_key or not tier_up:
            return await interaction.response.send_message(
                "❌ Zadej platný název kitu a tieru.", ephemeral=True
            )

        roles_map = load_data(KIT_ROLES_FILE, {})
        kit_map = roles_map.setdefault(kit_key, {})
        kit_map[tier_up] = str(role.id)
        save_data(KIT_ROLES_FILE, roles_map)

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
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        kit="Název kitu (např. MolePVP)",
        tier="Tier (např. S, A, B)",
    )
    async def unsetkitrole(
        self,
        interaction: discord.Interaction,
        kit: str,
        tier: str,
    ) -> None:
        kit_key = kit.strip().lower()
        tier_up = tier.strip().upper()
        roles_map = load_data(KIT_ROLES_FILE, {})
        kit_map = roles_map.get(kit_key, {})

        if tier_up not in kit_map:
            return await interaction.response.send_message(
                f"❌ Pro kit **{kit.strip()}** a tier **{tier_up}** žádné "
                "mapování není.",
                ephemeral=True,
            )

        del kit_map[tier_up]
        if not kit_map:
            roles_map.pop(kit_key, None)
        save_data(KIT_ROLES_FILE, roles_map)

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
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
import re

import discord
from discord import app_commands
from discord.ext import commands

from storage import load_data, save_data
from utils import has_tester_role, kit_autocomplete

log = logging.getLogger("dachshundtiers")

KIT_ROLES_FILE = "kit_roles.json"

# Role, které vypadají jako tier role (i kdyby nebyly v kit_roles.json):
# LT3, HT1, RLT5, RHT2, „UHCMace HT3", „HT3 (UHCMace)" …
_TIER_ROLE_RE = re.compile(r"(?i)(?:^|\W)(?:R?LT|R?HT)[1-5](?:\W|$)")


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
    @app_commands.autocomplete(kit=kit_autocomplete)
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
    @app_commands.autocomplete(kit=kit_autocomplete)
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

    # ------------------------------------------------------------------
    # /checkweb – projede hráče s tier rolemi a zkontroluje registraci na webu
    # ------------------------------------------------------------------
    @app_commands.command(
        name="checkweb",
        description="Projede hráče s tier rolemi a zkontroluje, jestli jsou registrovaní na webu",
    )
    async def checkweb(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        # 1) Tier role = role namapované v kit_roles.json + role se jménem
        #    jako tier (LT3, HT1, RLT5, „UHCMace HT3" …).
        roles_map = load_data(KIT_ROLES_FILE, {})
        mapped_ids = {
            str(rid)
            for kit_map in roles_map.values()
            for rid in (kit_map or {}).values()
            if rid
        }
        tier_roles = [
            r
            for r in guild.roles
            if str(r.id) in mapped_ids or _TIER_ROLE_RE.search(r.name)
        ]

        # 2) Členové serveru (zkusíme načíst plný seznam, jinak cache)
        members = list(guild.members)
        try:
            fetched = await guild.fetch_members().flatten()
            if fetched:
                members = fetched
        except Exception:  # noqa: BLE001 – bez members intentu fallback na cache
            pass

        players_with_tier_role = [
            m
            for m in members
            if not m.bot and any(r in m.roles for r in tier_roles)
        ]

        # 3) Webová data = players.json (posílá se na GitHub / na web)
        players = load_data("players.json") or []
        web_names = {str(p.get("username", "")).strip().lower() for p in players}

        missing = []
        registered = 0
        for m in players_with_tier_role:
            candidates = {m.nick, m.display_name, m.name}
            if any(
                (c or "").strip().lower() in web_names
                for c in candidates
                if c and c.strip()
            ):
                registered += 1
            else:
                missing.append(m)

        total = len(players_with_tier_role)
        embed = discord.Embed(
            title="🔎 Registrace na webu (players.json)",
            description=(
                f"Hráčů s tier rolí: **{total}**\n"
                f"✅ Na webu: **{registered}**\n"
                f"❌ Chybí na webu: **{len(missing)}**"
            ),
            color=0x10B981 if not missing else 0xEF4444,
        )
        if missing:
            lines = []
            for m in missing[:25]:
                roles_str = ", ".join(
                    r.name for r in m.roles if r in tier_roles
                )
                lines.append(f"• {m.mention} — `{m.display_name}` ({roles_str})")
            if len(missing) > 25:
                lines.append(f"…a dalších {len(missing) - 25}")
            embed.add_field(
                name="❌ Neregistrovaní na webu",
                value="\n".join(lines),
                inline=False,
            )
            embed.set_footer(
                text="Tip: chybějícího hráče zaregistruješ přes /result. "
                "Hráč se hledá podle nicku/IGN na serveru."
            )
        else:
            embed.add_field(
                name="🎉",
                value="Všichni hráči s tier rolí jsou zaregistrovaní na webu!",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Roles(bot))
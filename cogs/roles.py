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
- /checkweb    – bezpečná synchronizace webu (preview / apply), viz
                 ``cogs/checkweb.py`` (tato verze NIKDY nepíše automaticky).

Po `/result` se hráči automaticky dá role nového tieru a odeberou se ostatní
tier role stejného kitu.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.kit_roles import (
    get_all_kit_role_maps,
    get_kit_role_map,
    set_kit_role,
    unset_kit_role,
)
from services.permissions import has_admin_role, has_tester_role
from utils import kit_autocomplete

log = logging.getLogger("dachshundtiers")


@dataclass(frozen=True)
class TierRoleGrant:
    """Výsledek atomického udělení tier role (single ``member.edit``).

    ``ambiguous`` (H6 audit fix): ``True`` znamená, že se ``ok=False``
    vrátilo NE proto, že mutace jistě selhala (Forbidden, chybějící
    mapování/role/člen), ale protože klientský request selhal (timeout /
    spojení) A následné ověření aktuálního stavu Discordu se také
    nepodařilo — tedy nevíme, jestli Discord roli ve skutečnosti změnil.
    Volající NESMÍ v tomhle případě předpokládat ani úspěch, ani neúspěch
    (nikdy nepsat PG mirror jako potvrzený, nikdy neprovádět inverzní
    opravu) a měl by nechat pozdější pozorování/``/sync discord`` rozhodnout.
    """

    ok: bool
    note: str = ""
    tier_role_id: Optional[int] = None
    ambiguous: bool = False


async def auto_grant_kit_role(
    guild: discord.Guild,
    member_id: str,
    kit_key: str,
    tier_up: str,
    *,
    session_factory=None,
) -> TierRoleGrant:
    """Atomicky nastaví hráči roli tieru kitu (jediné ``member.edit``).

    Discord je jedinou autoritou aktuálních tierů – role se mění JEDNÍM
    API voláním (žádné add_roles + remove_roles zvlášť, žádný mezistav).
    Vrací :class:`TierRoleGrant` – ``ok=False`` znamená, že se role
    NEzměnila (volající pak nesmí zapisovat DB mirror jako potvrzený).
    """
    kit_key = (kit_key or "").strip().lower()
    kit_map = await get_kit_role_map(kit_key, session_factory=session_factory)
    if not kit_map:
        return TierRoleGrant(ok=False)

    role_id = kit_map.get(tier_up)
    if role_id is None:
        return TierRoleGrant(
            ok=False,
            note=(
                f"\n💡 Pro kit **{kit_key}** nemáš namapovanou roli tieru **{tier_up}** "
                f"– nastav ji přes `/setkitrole kit:{kit_key} tier:{tier_up} role:@Role`."
            ),
        )

    member = guild.get_member(int(member_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(member_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    if member is None:
        return TierRoleGrant(
            ok=False,
            note=(
                f"\n⚠️ Role **<@&{role_id}>** se nedá dát – hráč <@{member_id}> není na serveru."
            ),
        )

    role_obj = guild.get_role(int(role_id))
    if role_obj is None:
        log.warning("Role %s pro kit %s tier %s neexistuje", role_id, kit_key, tier_up)
        return TierRoleGrant(
            ok=False,
            note=(
                f"\n⚠️ Role <@&{role_id}> se nenašla – smaž ji a namapuj znovu přes "
                "`/setkitrole`."
            ),
        )

    kit_other_tier_ids = {
        int(rid) for tier, rid in kit_map.items() if tier != tier_up
    }
    target = [r for r in member.roles if r.id not in kit_other_tier_ids]
    if role_obj not in target:
        target.append(role_obj)
    if {r.id for r in member.roles} == {r.id for r in target}:
        return TierRoleGrant(ok=True, note="", tier_role_id=int(role_id))

    try:
        await member.edit(roles=target)
        return TierRoleGrant(
            ok=True,
            note=f"\n🎖️ Hráči byla dána role **{role_obj.mention}**.",
            tier_role_id=int(role_id),
        )
    except discord.Forbidden as err:
        # Definitive: the bot lacks permission — the mutation certainly did
        # not happen, no ambiguity, never worth re-verifying.
        log.warning("Nelze udělit roli %s pro %s: %s", role_id, member_id, err)
        return TierRoleGrant(
            ok=False,
            note=(
                f"\n⚠️ Roli <@&{role_id}> se nepovedlo udělit – zkontroluj oprávnění "
                "bota (Manage Roles a hierarchii rolí)."
            ),
        )
    except (discord.HTTPException, asyncio.TimeoutError, OSError) as err:
        # H6 audit fix: an HTTP-level failure, client-side timeout, or
        # connection error does NOT mean the edit definitely failed —
        # Discord may have applied it before the response/connection was
        # lost. Re-fetch the member and check whether the intended role set
        # actually landed before concluding anything.
        log.warning(
            "Nejistá odpověď při udělování role %s pro %s (%s) – ověřuji "
            "aktuální stav rolí",
            role_id,
            member_id,
            err,
        )
        try:
            verified = await guild.fetch_member(int(member_id))
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            asyncio.TimeoutError,
            OSError,
        ):
            verified = None
        if verified is not None:
            held_after = {r.id for r in verified.roles}
            target_ids = {r.id for r in target}
            if held_after == target_ids:
                # Discord DID apply it — never treat a confirmed change as
                # a failure just because the original response was lost.
                return TierRoleGrant(
                    ok=True,
                    note=f"\n🎖️ Hráči byla dána role **{role_obj.mention}**.",
                    tier_role_id=int(role_id),
                )
        # Neither the original call nor re-verification could confirm the
        # outcome — report this as genuinely UNKNOWN, not as a plain
        # failure: the caller must never write a false PG mirror state and
        # must never attempt a blind inverse mutation based on this alone.
        return TierRoleGrant(
            ok=False,
            ambiguous=True,
            note=(
                f"\n⚠️ Roli <@&{role_id}> se nepovedlo potvrdit (timeout) a ani "
                "zpětně ověřit – stav je NEJISTÝ. Zkontroluj ručně nebo spusť "
                "`/sync discord` (Discord zůstává jediným zdrojem pravdy)."
            ),
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

        ok = await set_kit_role(
            kit_key,
            tier_up,
            role.id,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )
        if not ok:
            return await interaction.response.send_message(
                f"❌ Kit **{kit_name}** (nebo tier **{tier_up}**) není registrovaný "
                "– namapování nelze uložit.",
                ephemeral=True,
            )

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

        removed = await unset_kit_role(
            kit_key,
            tier_up,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )
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

        roles_map = await get_all_kit_role_maps(
            session_factory=getattr(self.bot, "db_session_factory", None)
        )
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
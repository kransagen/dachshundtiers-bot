"""Správa automatických rolí kitů a tierů po `/result`.

Mapování žije v PostgreSQL (``kit_roles`` – services.kit_roles; host-local
`data/kit_roles.json` se používá jen jako startup validace serverových role
ID, viz db.validation)::

    "molepvp": {"S": "123456789012345678", "A": "123456789012345679"},

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

    ``ok=True`` znamená jen „role nebyla odmítnuta". Aby se ale mohla
    zapsat do PostgreSQL jako POTVRZENÝ stav, musí být ``verified=True``:
    G0 cutover (invariant 6) vyžaduje mezi mutací Discordu a commitem do DB
    **ověřit skutečný finální stav rolí**. ``verified`` je defaultně
    ``False``, takže každý nový návratový bod, který zapomene stav
    potvrdit, selže bezpečně (nic se necommitne) místo tichého předpokladu.

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
    verified: bool = False


def _role_ids(member) -> Optional[set[int]]:
    """Sada ID rolí člena, nebo ``None``, pokud objekt role nepopisuje.

    ``None`` znamená „stav se nedá z tohoto objektu přečíst" — nikdy „stav je
    prázdný". Rozlišení je zásadní: ``None`` nesmí být zaměněno za shodu se
    zamýšlenou množinou rolí.
    """
    roles = getattr(member, "roles", None)
    if not isinstance(roles, (list, tuple, set, frozenset)):
        return None
    ids: set[int] = set()
    for role in roles:
        role_id = getattr(role, "id", None)
        if not isinstance(role_id, int):
            return None
        ids.add(role_id)
    return ids


async def _read_current_role_ids(guild, member_id: str) -> Optional[set[int]]:
    """Živý dotaz na aktuální role člena — autoritativní čtení z Discordu.

    ``None`` = stav se nepodařilo přečíst (NotFound / Forbidden / HTTP /
    timeout / connection error). Volající to MUSÍ chápat jako „neznámé",
    nikdy jako „nezměněné" ani jako „potvrzené".
    """
    try:
        fresh = await guild.fetch_member(int(member_id))
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
        asyncio.TimeoutError,
        OSError,
    ):
        return None
    return _role_ids(fresh)


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

    G0 cutover: ``ok=True`` je doplněno o ``verified=True`` **jen když byl
    skutečný finální stav rolí potvrzen** (z odpovědi PATCHu, jinak živým
    dotazem). Bez potvrzení vrátí ``ok=True, verified=False`` / ambiguous,
    takže PostgreSQL nikdy nedostane stav, který Discord nepotvrdil.
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
    target_ids = {r.id for r in target}
    role_mention = role_obj.mention

    if _role_ids(member) == target_ids:
        # Cached state already matches — nothing to mutate. But the cache can
        # be stale and Discord is the authority, so confirm against a live
        # read before reporting a CONFIRMED state the PG mirror may store.
        confirmed = await _read_current_role_ids(guild, member_id)
        if confirmed == target_ids:
            return TierRoleGrant(
                ok=True, note="", tier_role_id=int(role_id), verified=True
            )
        if confirmed is None:
            return TierRoleGrant(
                ok=False,
                ambiguous=True,
                tier_role_id=int(role_id),
                note=(
                    f"\n⚠️ Roli <@&{role_id}> už hráč má, ale aktuální stav rolí se "
                    "nepodařilo z Discordu načíst – stav je NEJISTÝ, do PostgreSQL "
                    "se proto nic nezapsalo. Zkontroluj ručně nebo spusť "
                    "`/sync discord` (Discord zůstává jediným zdrojem pravdy)."
                ),
            )
        # Cache was stale (live read disagrees) — fall through and mutate.

    try:
        updated = await member.edit(roles=target)
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
        confirmed = await _read_current_role_ids(guild, member_id)
        if confirmed == target_ids:
            # Discord DID apply it — never treat a confirmed change as
            # a failure just because the original response was lost.
            return TierRoleGrant(
                ok=True,
                note=f"\n🎖️ Hráči byla dána role **{role_mention}**.",
                tier_role_id=int(role_id),
                verified=True,
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

    # ── Success path (G0, invariant 6) ──────────────────────────────────
    # "member.edit did not raise" is NOT a confirmation. The PATCH response
    # body already describes the member as Discord now sees it; when it is not
    # usable, fall back to a live re-read. Only a role-id set equal to the
    # intended one is ever reported as a confirmed state.
    observed = _role_ids(updated)
    if observed is None:
        observed = await _read_current_role_ids(guild, member_id)

    if observed == target_ids:
        return TierRoleGrant(
            ok=True,
            note=f"\n🎖️ Hráči byla dána role **{role_mention}**.",
            tier_role_id=int(role_id),
            verified=True,
        )
    if observed is None:
        # Mutation "succeeded" but the final state could not be read at all.
        return TierRoleGrant(
            ok=False,
            ambiguous=True,
            tier_role_id=int(role_id),
            note=(
                f"\n⚠️ Roli <@&{role_id}> se nepovedlo potvrdit a stav rolí se "
                "nepodařilo přečíst – stav je NEJISTÝ, do PostgreSQL se proto "
                "nic nezapsalo. Zkontroluj ručně nebo spusť `/sync discord` "
                "(Discord zůstává jediným zdrojem pravdy)."
            ),
        )
    # Discord answered with a DIFFERENT role set than requested — a confirmed
    # non-application (e.g. a concurrent edit won the race). Never report this
    # as success; PostgreSQL must not mirror a state Discord never confirmed.
    log.warning(
        "Udělení role %s pro %s: Discord vrátil jinou množinu rolí (%s != %s)",
        role_id,
        member_id,
        sorted(observed),
        sorted(target_ids),
    )
    return TierRoleGrant(
        ok=False,
        tier_role_id=int(role_id),
        note=(
            f"\n⚠️ Discord potvrdil jiný stav rolí, než bylo požadováno – "
            f"role <@&{role_id}> se v záznamu nepovažuje za potvrzenou. Zkontroluj "
            "roli ručně nebo spusť `/sync discord`."
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
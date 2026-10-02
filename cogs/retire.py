"""``/retire`` a ``/peaktier`` – viz ``services/retire_peak`` (pravidla a výpočty).

* ``/retire``   – hráč sám; bot ověří nárok, po potvrzení sundá kit roli
  aktuálního tieru (a přidá roli retired tieru, pokud je namapovaná) a teprve
  pak zapíše retire do databáze (Discord je autorita, viz mirror sync).
* ``/peaktier`` – jen zobrazí zapsané peaky a kolik zbývá do dalších.
* Peaky zapisuje bot sám hodinovou úlohou ``sweep_peaks``.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._shared import apply_role_actions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from db.services.retire_peak import (
    Progress,
    RetirePlan,
    RetireRefused,
    commit_retire,
    plan_retire,
    player_peaks,
    player_progress,
    sweep_peaks,
)
from utils import kit_autocomplete

log = logging.getLogger("dachshundtiers")


def _progress_line(p: Progress) -> str:
    if p.eligible:
        return f"**{p.kit_name}** · {p.tier_code} – ✅ podmínky splněny ({p.days}/{p.days_needed} dní)"
    parts = [f"zbývá **{p.days_left} dní** ({p.days}/{p.days_needed})"]
    if p.wins_needed is not None:
        parts.append(f"nebo **{p.wins_left} výher** ({p.wins}/{p.wins_needed})")
    return f"**{p.kit_name}** · {p.tier_code} – " + " ".join(parts)


def _missing_retired_role(plan: RetirePlan) -> str:
    return (
        f"❌ Pro **{plan.retired_tier_code}** v kitu **{plan.kit_name}** není "
        "namapovaná Discord role – retire zatím nelze provést, napiš adminovi."
    )


class RetireConfirmView(discord.ui.View):
    def __init__(self, cog: "Retire", user_id: int, plan: RetirePlan):
        super().__init__(timeout=120)
        self.cog, self.user_id, self.plan = cog, user_id, plan

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Potvrdit retire", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="⏳ Zpracovávám…", view=None)
        await interaction.followup.send(
            await self.cog.execute_retire(interaction, self.plan), ephemeral=True
        )

    @discord.ui.button(label="Zrušit", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Zrušeno.", view=None)


class Retire(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _sf(self):
        return getattr(self.bot, "db_session_factory", None)

    async def cog_load(self) -> None:
        self.peak_sweep.start()

    async def cog_unload(self) -> None:
        self.peak_sweep.cancel()

    @tasks.loop(hours=1)
    async def peak_sweep(self) -> None:
        if self._sf() is None:
            return
        try:
            granted = await sweep_peaks(self._sf())
            if granted:
                log.info("Peak tier sweep: zapsáno %d nových peaků", granted)
        except Exception:  # noqa: BLE001 – úloha se nesmí zastavit
            log.exception("Peak tier sweep selhal")

    @peak_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def execute_retire(self, interaction: discord.Interaction, plan: RetirePlan) -> str:
        member = interaction.user
        try:
            fresh = await plan_retire(self._sf(), member.id, plan.kit_name)
        except RetireRefused as err:
            return str(err)
        if fresh.tier_id != plan.tier_id:
            return "❌ Tvůj tier se mezitím změnil – zopakuj `/retire`."
        if not plan.grant_discord_role:
            return _missing_retired_role(plan)
        actions = []
        if plan.revoke_discord_role and any(r.id == plan.revoke_discord_role for r in member.roles):
            actions.append({"op": "remove", "role_id": plan.revoke_discord_role})
        if plan.grant_discord_role:
            actions.append({"op": "add", "role_id": plan.grant_discord_role})
        for action in actions:
            action.update(member_id=member.id, member_name=str(member))
        applied = await apply_role_actions(interaction.guild, actions)
        if not all(a["ok"] for a in applied):
            log.error("/retire: změna rolí selhala (user %s): %s", member.id, applied)
            return "❌ Nepodařilo se změnit role na Discordu, retire se neprovedl."
        try:
            await commit_retire(self._sf(), plan, actor_name=str(member))
        except RetireRefused as err:
            log.error("/retire: plán už neplatí po změně rolí (user %s): %s", member.id, err)
            return f"{err}\n⚠️ Role na Discordu už jsou změněné – napiš adminovi."
        except Exception:  # noqa: BLE001
            log.exception("/retire: zápis do DB selhal po změně rolí (user %s)", member.id)
            return (
                "⚠️ Role na Discordu jsou změněné, ale zápis do databáze selhal. "
                "Napiš adminovi."
            )
        return (
            f"✅ **{plan.kit_name}**: {plan.tier_code} → **{plan.retired_tier_code}**. "
            f"Tvůj peak tier **{plan.tier_code}** je uložený navždy."
        )

    @app_commands.command(name="retire", description="Retire z tieru LT2/HT2/LT1/HT1 v kitu")
    @app_commands.describe(kit="Kit, ve kterém chceš odejít do retire")
    @app_commands.autocomplete(kit=kit_autocomplete)
    async def retire(self, interaction: discord.Interaction, kit: str) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message("❌ Pouze na serveru.", ephemeral=True)
        if self._sf() is None:
            return await interaction.response.send_message(
                "❌ Dostupné jen s PostgreSQL backendem.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            plan = await plan_retire(self._sf(), interaction.user.id, kit)
        except RetireRefused as err:
            return await interaction.followup.send(str(err), ephemeral=True)
        if not plan.grant_discord_role:
            return await interaction.followup.send(
                _missing_retired_role(plan), ephemeral=True
            )
        await interaction.followup.send(
            f"Opravdu chceš v kitu **{plan.kit_name}** odejít z **{plan.tier_code}** "
            f"do **{plan.retired_tier_code}**? Zpět to jde jen unretire testem.",
            view=RetireConfirmView(self, interaction.user.id, plan),
            ephemeral=True,
        )

    @app_commands.command(
        name="peaktier", description="Tvoje peak tiery a kolik zbývá do dalších"
    )
    async def peaktier(self, interaction: discord.Interaction) -> None:
        if self._sf() is None:
            return await interaction.response.send_message(
                "❌ Dostupné jen s PostgreSQL backendem.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        async with transaction(self._sf()) as session:
            player = await PlayerRepository().get_by_discord_id(session, interaction.user.id)
            if player is None:
                return await interaction.followup.send(
                    "ℹ️ Nemáš v databázi žádný záznam – propoj se přes `/linkign`.",
                    ephemeral=True,
                )
            peaks = await player_peaks(session, player)
            progress = await player_progress(session, player)
        held = {kit for kit, _ in peaks}
        lines = ["**Zapsané peaky**"] + (
            [f"• **{kit}** – {tier}" for kit, tier in peaks] or ["• zatím žádné"]
        )
        pending = [p for p in progress if not (p.eligible and p.kit_name in held)]
        if pending:
            lines += ["", "**Do dalšího peaku**"] + [f"• {_progress_line(p)}" for p in pending]
        lines += ["", "_Peak se zapisuje automaticky (kontrola každou hodinu)._"]
        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Retire(bot))

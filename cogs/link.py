"""Propojení Discord ↔ Minecraft: ``/link``, ``/linked``, ``/unlink``.

Player self-service, no admin role required — a player links their OWN
account. The commands deliberately do **not** take a UUID:

* ``/link``    → issues a one-time, expiring code (see
  ``services/minecraft_link.start_link``),
* ``/linked``  → shows the current link state and whether a code is pending,
* ``/unlink``  → drops the link (the Minecraft account row is kept).

Completing a link needs proof that the caller owns the Minecraft account, and
that proof can only come from the Minecraft side. ``services.minecraft_link
.complete_link`` is that entry point and is deliberately NOT reachable from a
Discord command: accepting a UUID typed by the caller would let anyone claim
any account, which is not ownership proof.

The UUID is the identity. ``Player.ign`` is only the display name the rest of
the business logic uses; "is this the same person" is answered by the
database-enforced one-to-one link.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.minecraft_link import (
    LinkError,
    LINK_TOKEN_TTL,
    link_status,
    mask_uuid,
    start_link,
    unlink,
)
from services.player_link import (
    LINK_ADOPTED,
    LINK_CREATED,
    LINK_MERGED,
    LINK_RENAMED,
    LinkRefused,
    link_ign,
    linked_ign,
)

log = logging.getLogger("dachshundtiers")


class MinecraftLink(commands.Cog):
    """``/link``, ``/linked``, ``/unlink`` — Discord ↔ Minecraft identity."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _sf(self):
        return getattr(self.bot, "db_session_factory", None)

    @app_commands.command(
        name="linkign",
        description="Propojí tvůj Discord účet s tvým Minecraft jménem (nutné pro frontu a tickety)",
    )
    @app_commands.describe(ign="Tvoje Minecraft jméno (IGN)")
    async def linkign(self, interaction: discord.Interaction, ign: str) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        session_factory = self._sf()
        try:
            outcome = await link_ign(
                interaction.user.id,
                ign,
                session_factory=session_factory,
                actor_name=str(interaction.user),
            )
        except LinkRefused as err:
            return await interaction.followup.send(str(err), ephemeral=True)

        await mirror_member(session_factory, interaction.user, command="/linkign")
        text = {
            LINK_CREATED: "✅ Propojeno: tvůj Discord je teď **{ign}**.",
            LINK_ADOPTED: "✅ Propojeno s existujícím záznamem **{ign}** i s historií tierů.",
            LINK_MERGED: "✅ Propojeno s **{ign}** a tvoje dřívější data se k němu přesunula.",
            LINK_RENAMED: "✅ IGN změněno na **{ign}**.",
        }.get(outcome.status, "ℹ️ Už jsi propojený jako **{ign}** – nic se neměnilo.")
        await interaction.followup.send(
            text.format(ign=outcome.ign)
            + "\nTeď se můžeš připojit do fronty i otevřít HT3+ ticket.",
            ephemeral=True,
        )

    @app_commands.command(
        name="link",
        description="Vytvoří jednorázový kód pro propojení Minecraft účtu",
    )
    async def link(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )

        session_factory = self._sf()
        if session_factory is None:
            # No silent fallback: without a database there is nowhere to
            # record a link, and a fake success would be worse than a refusal.
            return await interaction.response.send_message(
                "❌ Propojování je dostupné jen s PostgreSQL backendem.", ephemeral=True
            )

        try:
            code = await start_link(interaction.user.id, session_factory=session_factory)
        except LinkError as err:
            return await interaction.response.send_message(
                f"❌ {err.message}", ephemeral=True
            )

        minutes = int(LINK_TOKEN_TTL.total_seconds() // 60)
        await interaction.response.send_message(
            f"🔗 Tvůj propojovací kód je **`{code}`**.\n\n"
            f"Platí {minutes} minut a jde použít **jednou**. Kód zadej ze strany "
            "Minecraft serveru (nebo přes jeho plugin) jako jediný způsob, jak "
            "dokázat, že účet vlastníš. Když už budeš propojený, kód tě smaže.",
            ephemeral=True,
        )

    @app_commands.command(
        name="linked",
        description="Ukaže, s jakým Minecraft účtem jsi propojený",
    )
    async def linked(self, interaction: discord.Interaction) -> None:
        session_factory = self._sf()
        if session_factory is None:
            return await interaction.response.send_message(
                "❌ Propojování je dostupné jen s PostgreSQL backendem.", ephemeral=True
            )

        ign = await linked_ign(interaction.user.id, session_factory=session_factory)
        status = await link_status(interaction.user.id, session_factory=session_factory)
        if ign and not status.linked:
            return await interaction.response.send_message(
                f"✅ Jsi propojený jako **{ign}**. Změna IGN: `/linkign <nové IGN>`.",
                ephemeral=True,
            )
        if not status.discord_id and not status.linked:
            return await interaction.response.send_message(
                "Zatím nejsi propojený. Spusť `/linkign <tvoje IGN>`.", ephemeral=True
            )

        if not status.linked:
            pending = ""
            if status.pending_code_expires_at is not None:
                pending = (
                    "\n⏳ Máš rozjetý propojovací kód (vyprší "
                    f"{status.pending_code_expires_at:%H:%M})."
                )
            return await interaction.response.send_message(
                f"Zatím nejsi propojený.{pending}", ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Jsi propojený.\n"
            f"• Minecraft UUID: `{mask_uuid(status.uuid)}`\n"
            f"• Jméno: `{status.name or status.ign or '—'}`\n"
            f"• Discord: <@{interaction.user.id}>\n\n"
            "Pro změnu účtu použij `/unlink` a pak `/link` znovu.",
            ephemeral=True,
        )

    @app_commands.command(
        name="unlink",
        description="Zruší propojení Discord ↔ Minecraft účtu",
    )
    async def unlink_cmd(self, interaction: discord.Interaction) -> None:
        session_factory = self._sf()
        if session_factory is None:
            return await interaction.response.send_message(
                "❌ Propojování je dostupné jen s PostgreSQL backendem.", ephemeral=True
            )

        if await unlink(interaction.user.id, session_factory=session_factory):
            await interaction.response.send_message(
                "✅ Propojení zrušeno. IGN zůstává uložený, aby ti šly fungovat "
                "fronty a testy; propojený Minecraft účet už ale není.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Zatím nemáš žádné propojení.", ephemeral=True
            )


async def mirror_member(session_factory, member, *, command: str) -> None:
    """Hned po propojení zapíše tier role hráče do DB (nečeká na hodinovou
    reconciliation). Selhání jen zaloguje – propojení už je uložené."""
    from db.services.mirror_sync import DiscordSyncService

    if not hasattr(member, "roles"):
        return
    try:
        await DiscordSyncService().sync_guild(
            session_factory,
            members=[member],
            command=command,
            triggered_by=member.id,
            triggered_by_name=str(member),
        )
    except Exception:  # noqa: BLE001
        log.exception("Zrcadlení rolí po %s selhalo", command)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MinecraftLink(bot))

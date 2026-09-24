"""Diagnostický cog – /verze ukáže, na jaké verzi bot reálně běží."""

import logging
import subprocess

import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_ID

log = logging.getLogger("dachshundtiers")


def git_commit() -> str:
    """Vrátí short hash aktuálního commitu repozitáře (fallback '??')."""
    try:
        output = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return output or "??"
    except Exception:  # noqa: BLE001
        return "??"


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="verze",
        description="Zobrazí commit a stav /result (diagnostika)",
    )
    async def verze(self, interaction: discord.Interaction) -> None:
        commit = git_commit()

        tree = self.bot.tree
        commands_list = tree.get_commands()
        result_cmd = next(
            (c for c in commands_list if c.name == "result"), None
        )
        has_add_role = bool(
            result_cmd is not None
            and any(p.name == "add_role" for p in result_cmd.parameters)
        )

        # Discord-side stavy (read-only) – rozliší lokální x globální x guild
        # a odhalí duplicitní jména (stejný příkaz v globálním i guild scope).
        disc_global = disc_guild = None
        try:
            disc_global = await tree.fetch_commands()
            if GUILD_ID:
                disc_guild = await tree.fetch_commands(
                    guild=discord.Object(id=GUILD_ID)
                )
        except Exception:  # noqa: BLE001
            disc_global = disc_guild = None

        def _fmt(seq) -> str:
            return f"{len(seq)}" if seq is not None else "nedostupné"

        if disc_global is not None and disc_guild is not None:
            glob_names = {c.name for c in disc_global}
            guild_names = {c.name for c in disc_guild}
            duplicates = sorted(glob_names & guild_names)
        else:
            duplicates = None

        dup_text = (
            "žádné"
            if duplicates is not None and not duplicates
            else "nedostupné"
            if duplicates is None
            else ", ".join(f"`{n}`" for n in duplicates)
        )

        embed = discord.Embed(
            title="ℹ️ Verze bota",
            color=discord.Color.blurple(),
            description=(
                f"**Commit:** `{commit}`  "
                f"(`{'nová verze' if commit != '??' else 'git nedostupný'}`)\n"
                f"**/result `add_role` / `remove_role`:** "
                f"{'✅ ano' if has_add_role else '❌ ne'}\n"
                f"**Slash – lokální (tree):** {len(commands_list)}\n"
                f"**Slash – Discord globální:** {_fmt(disc_global)}\n"
                f"**Slash – Discord guild:** {_fmt(disc_guild)}\n"
                f"**Duplicitní jména (global ∩ guild):** {dup_text}"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Info(bot))
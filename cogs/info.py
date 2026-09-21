"""Diagnostický cog – /verze ukáže, na jaké verzi bot reálně běží."""

import logging
import subprocess

import discord
from discord import app_commands
from discord.ext import commands

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

        commands_list = self.bot.tree.get_commands()
        result_cmd = next(
            (c for c in commands_list if c.name == "result"), None
        )
        has_add_role = bool(
            result_cmd is not None
            and any(p.name == "add_role" for p in result_cmd.parameters)
        )

        embed = discord.Embed(
            title="ℹ️ Verze bota",
            color=discord.Color.blurple(),
            description=(
                f"**Commit:** `{commit}`  "
                f"(`{'nová verze' if commit != '??' else 'git nedostupný'}`)\n"
                f"**/result `add_role` / `remove_role`:** "
                f"{'✅ ano' if has_add_role else '❌ ne'}\n"
                f"**Počet slash příkazů:** {len(commands_list)}"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Info(bot))
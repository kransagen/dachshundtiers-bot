"""Cog s HT Fight výsledky – /topresult.

``/topresult`` je specializovaná verze ``/result`` pro HT Fighty – **NENÍ** to
žebříček a nepočítá žádné „top" hráče. Vytvoří veřejný výsledek HT Fightu
přesně ve stylu serveru do vyhrazeného kanálu (``TOP_RESULT_CHANNEL_ID``) a
zapinguje nakonfigurovanou roli (``TOP_RESULT_ROLE_ID``, pouze ta!):

    <@1419031701920940163> - mendu__ - **Zůstává Low Tier 3** - MolePVP

    **HT3 Fighty:**
    > prohrál 0-4 <@1018169843347882076>

    <@&1523984977371594772>

Behavior:
- záznam jde do STEJNÉ kanonické historie jako /result (``data/ht_results.json``)
  s ``resultType: "ht_fight"`` – žádná samostatná databáze,
- **nikdy nemění tier hráče** (players.json se nedotýká; změna tieru po HT
  Fightu by musela projít kanonickou logikou /result – ta v projektu zatím
  není definovaná),
- příkaz NIKDY nepoužije ``RESULT_CHANNEL_LOWER`` / ``RESULT_CHANNEL_UPPER``,
- v kanálu HT Fight ticketu se hráč/IGN/kit berou z ticketu (autoritativní),
  idempotence = ``ticketId + result_type`` (druhé odeslání → jasná chyba),
- oprávnění: stejný model jako /result (tester role).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import TOP_RESULT_CHANNEL_ID, TOP_RESULT_ROLE_ID
from services.tickets import get_ticket, is_ht_fight_ticket
from services.topresult import (
    HT_FIGHT_TIERS,
    format_topresult_message,
    is_registered_kit,
    record_ht_fight,
    validate_ht_fight_score,
    validate_ht_fight_status,
    validate_ht_fight_tier,
    validate_topresult_config,
)
from utils import get_kits, has_tester_role, kit_autocomplete, now_ms, today_cz

log = logging.getLogger("dachshundtiers")


async def fight_tier_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete HT Fight tieru (reálné tiery žebříčku, bez LT3E)."""
    text = (current or "").lower()
    out = []
    for t in HT_FIGHT_TIERS:
        if not text or text in t.lower():
            out.append(app_commands.Choice(name=t, value=t))
    return out[:25]


class TopResult(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        """Zachytí neošetřené chyby příkazu – pošle hlášku a zaloguje."""
        log.exception("Chyba v příkazu %s: %s", interaction.command, error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # /topresult
    # ------------------------------------------------------------------
    @app_commands.command(name="topresult", description="HT Fight výsledek – veřejná zpráva + role ping")
    @app_commands.describe(
        hrac="Testovaný hráč",
        ign="Minecraft IGN hráče",
        kit="Kit (v HT Fight ticketu se použije z ticketu)",
        fight_tier="HT Fight tier (např. HT3)",
        outcome="Výsledek hráče (vyhrál / prohrál)",
        score="Skóre ve formátu 0-4",
        opponent="Soupeř / tester",
        tier_status="Text stavu tieru (např. Zůstává Low Tier 3)",
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="vyhrál", value="Won"),
            app_commands.Choice(name="prohrál", value="Lost"),
        ]
    )
    @app_commands.autocomplete(kit=kit_autocomplete, fight_tier=fight_tier_autocomplete)
    async def topresult(
        self,
        interaction: discord.Interaction,
        hrac: discord.User,
        ign: str,
        kit: str,
        fight_tier: str,
        outcome: str,
        score: str,
        opponent: discord.User,
        tier_status: str,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        # 0) Konfigurace /topresult – jasná admin chyba místo tichého selhání.
        ok_cfg, cfg_msg = validate_topresult_config(TOP_RESULT_CHANNEL_ID, TOP_RESULT_ROLE_ID)
        if not ok_cfg:
            return await interaction.followup.send(cfg_msg, ephemeral=True)

        # 0a) Cílový kanál + role musí existovat (role se NIKDY nebere od
        #     uživatele – jen nakonfigurovaná TOP_RESULT_ROLE_ID).
        result_channel = self.bot.get_channel(TOP_RESULT_CHANNEL_ID)
        if result_channel is None and interaction.guild is not None:
            try:
                result_channel = await interaction.guild.fetch_channel(TOP_RESULT_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                result_channel = None
        if result_channel is None:
            return await interaction.followup.send(
                f"❌ Kanál pro HT Fight výsledky (<#{TOP_RESULT_CHANNEL_ID}>) "
                "nebyl nalezen – zkontroluj `TOP_RESULT_CHANNEL_ID`.",
                ephemeral=True,
            )
        target_role = (
            interaction.guild.get_role(TOP_RESULT_ROLE_ID)
            if interaction.guild is not None
            else None
        )
        if target_role is None:
            return await interaction.followup.send(
                f"❌ Role pro HT Fight ping (ID `{TOP_RESULT_ROLE_ID}`) nebyla "
                "na serveru nalezena – zkontroluj `TOP_RESULT_ROLE_ID`.",
                ephemeral=True,
            )

        # 0b) HT Fight ticket? V kanálu ticketu jsou metadata ticketu
        #     autoritativní (hráč, IGN, kit) – uživatelské hodnoty se nepřepisují.
        ticket = None
        if isinstance(interaction.channel, discord.TextChannel):
            ticket = await get_ticket(interaction.channel_id)

        player_id = str(hrac.id)
        player_name = hrac.display_name or hrac.name
        ign_clean = ign.strip()
        kit_clean = kit.strip()

        if ticket is not None:
            if not is_ht_fight_ticket(ticket):
                return await interaction.followup.send(
                    "❌ /topresult se používá v **HT Fight ticketu**. Tento kanál "
                    "je HT ticket, ale není typu „fight“ – HT Fight výsledek zadej "
                    "mimo ticket (nebo v HT Fight ticketu).",
                    ephemeral=True,
                )
            if ticket.get("status") != "open":
                return await interaction.followup.send(
                    "❌ HT Fight ticket je zavřený.", ephemeral=True
                )
            if str(ticket.get("ownerId", "")) != player_id:
                owner = ticket.get("ownerId")
                return await interaction.followup.send(
                    "❌ V HT Fight ticketu se zadává výsledek VLASTNÍKA ticketu. "
                    f"Tento ticket patří <@{owner}> – hráč v příkazu se neshoduje.",
                    ephemeral=True,
                )
            # Autoritativní data z ticketu.
            ign_clean = (ticket.get("ign") or ign_clean).strip()
            kit_clean = (ticket.get("kit") or kit_clean).strip()
        else:
            if not ign_clean:
                return await interaction.followup.send(
                    "❌ IGN hráče je povinné.", ephemeral=True
                )
            if not is_registered_kit(kit_clean, get_kits()):
                return await interaction.followup.send(
                    f"❌ Neznámý kit `{kit_clean}` – registruj ho přes `/addkit` "
                    "(nebo první `/result`).",
                    ephemeral=True,
                )

        # 0c) Validace vstupů (skóre, HT tier, status).
        ok, msg = validate_ht_fight_tier(fight_tier)
        if not ok:
            return await interaction.followup.send(msg, ephemeral=True)
        ok, msg = validate_ht_fight_score(score)
        if not ok:
            return await interaction.followup.send(msg, ephemeral=True)
        ok, msg = validate_ht_fight_status(tier_status)
        if not ok:
            return await interaction.followup.send(msg, ephemeral=True)
        if opponent.id == hrac.id:
            return await interaction.followup.send(
                "❌ Soupeř nemůže být stejný hráč jako testovaný.", ephemeral=True
            )

        # 1) Zápis do kanonické historie (ht_results.json, resultType=ht_fight).
        #    Idempotence u ticketu (ticketId + result_type) se hlídá atomicky
        #    uvnitř record_ht_fight – opakované odeslání nic nepošle dvakrát.
        record = await record_ht_fight(
            ticket_id=ticket["id"] if ticket is not None else None,
            player_id=player_id,
            player_name=player_name,
            ign=ign_clean,
            evaluator_id=str(interaction.user.id),
            evaluator_name=interaction.user.display_name,
            kit=kit_clean,
            fight_tier=fight_tier,
            score=score,
            outcome=outcome,
            opponent_id=str(opponent.id),
            opponent_name=opponent.display_name or opponent.name,
            tier_status=tier_status,
            now=now_ms(),
            date=today_cz(),
        )
        r = record["result"]
        if r == "duplicate":
            existing = record.get("existing") or {}
            return await interaction.followup.send(
                "❌ Výsledek pro tento HT Fight ticket už byl zaznamenán "
                f"(idempotentně – nic se nemění; skóre {existing.get('score') or '?'} "
                f"dne {existing.get('date') or '?'}).",
                ephemeral=True,
            )
        if r == "not_found":
            return await interaction.followup.send(
                "❌ Tento kanál není HT ticket.", ephemeral=True
            )
        if r == "not_fight_ticket":
            return await interaction.followup.send(
                "❌ Kanál je HT ticket, ale není typu „fight“ – HT Fight výsledky "
                "se zadávají mimo eval tickety.",
                ephemeral=True,
            )
        if r == "ticket_closed":
            return await interaction.followup.send(
                "❌ HT Fight ticket je zavřený.", ephemeral=True
            )
        if r == "wrong_player":
            owner = (record.get("ticket") or {}).get("ownerId")
            return await interaction.followup.send(
                "❌ Ticket patří jinému hráči "
                f"({f'<@{owner}>' if owner else 'neznámý'}).",
                ephemeral=True,
            )
        if r == "wrong_kit":
            kit_of = (record.get("ticket") or {}).get("kit")
            return await interaction.followup.send(
                f"❌ Kit neodpovídá ticketu – ticket je na kit **{kit_of}**.",
                ephemeral=True,
            )
        if r in (
            "invalid_argument",
            "invalid_tier",
            "invalid_score",
            "invalid_outcome",
            "invalid_status",
        ):
            return await interaction.followup.send(
                record.get("message", "❌ Neplatný výsledek."), ephemeral=True
            )
        if r != "created":
            return await interaction.followup.send(
                "❌ Výsledek se nepodařilo uložit.", ephemeral=True
            )

        # 2) Sestavení zprávy ve stylu serveru + odeslání do TOP_RESULT kanálu.
        #    Role ping jde POUZE na nakonfigurovanou TOP_RESULT_ROLE_ID
        #    (allowed_mentions.roles) – žádná uživatelská role se nepřijímá.
        message = format_topresult_message(
            player_id=player_id,
            ign=ign_clean,
            tier_status=tier_status.strip(),
            kit=kit_clean,
            fight_tier=fight_tier,
            outcome=outcome,
            score=score.strip(),
            opponent_id=str(opponent.id),
            role_id=TOP_RESULT_ROLE_ID,
        )
        allowed = discord.AllowedMentions(everyone=False, users=True, roles=[target_role])
        try:
            await result_channel.send(content=message, allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning(
                "Nelze poslat HT Fight výsledek do %s: %s", TOP_RESULT_CHANNEL_ID, err
            )
            return await interaction.followup.send(
                f"❌ HT Fight výsledek se nepodařilo odeslat do <#{TOP_RESULT_CHANNEL_ID}> "
                "(záznam ale zůstal v historii `ht_results.json`).",
                ephemeral=True,
            )

        await interaction.followup.send(
            f"✅ Výsledek HT Fightu (`{ign_clean}` · {kit_clean} · "
            f"**{fight_tier.strip().upper()} Fighty:** {score.strip()}) byl "
            f"zaznamenán a odeslán do <#{TOP_RESULT_CHANNEL_ID}>.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TopResult(bot))
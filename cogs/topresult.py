"""Cog s HT Fight výsledky – /topresult.

``/topresult`` je specializovaná verze ``/result`` pro HT Fighty – **NENÍ** to
žebříček a nepočítá žádné „top" hráče. Vytvoří veřejný výsledek HT Fightu
přesně ve stylu serveru do vyhrazeného kanálu (``TOP_RESULT_CHANNEL_ID``) a
zapinguje nakonfigurovanou roli (``TOP_RESULT_ROLE_ID``, pouze ta!):

    <@1419031701920940163> - mendu__ - **Povýšen na HT3** - MolePVP

    **HT3 Fighty:**
    > vyhrál 4-1 <@1018169843347882076>

    **Postup: LT3 → HT3**

    <@&1523984977371594772>

Behavior:
- záznam jde do STEJNÉ kanonické historie jako /result (``data/ht_results.json``)
  s ``resultType: "ht_fight"`` – žádná samostatná databáze,
- **výhra povyšuje hráče** na další tier (services/topresult.py – kanonické
  pravidlo next_ticket_tier); výhra udělí i novou tier roli (stejná logika
  jako /result – auto_grant_kit_role); prohra tier ani roli nemění,
- **bridge** (volitelný parametr, jen při výhře) – tester může hráče povýšit
  přeskočením rovnou na zadaný vyšší tier (např. topresult o získání HT3,
  ale hráč z LT3 bridgne přímo na LT2); cíl musí být reálný tier a strictly
  vyšší než aktuální tier hráče; do záznamu se připíše ``bridgeTier``,
- prohra uvnitř HT Fight ticketu ticket zavře + nastaví HT3+ cooldown;
  výhra ticket NEZAVÍRÁ ani cooldown nenastavuje (hráč ve výhře pokračuje),
- oznámení má stav pending → sent/failed (retry tlačítkem po selhání –
  záznam v historii zůstává append-only, mění se jen stav oznámení),
- příkaz NIKDY nepoužije ``RESULT_CHANNEL_LOWER`` / ``RESULT_CHANNEL_UPPER``,
- v kanálu HT Fight ticketu se hráč/IGN/kit berou z ticketu (autoritativní);
  mimo ticket jsou volitelné parametry hrac/ign/kit povinné,
- idempotence = ``ticketId + result_type`` (druhé odeslání → jasná chyba),
- oprávnění: stejný model jako /result (tester role).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    HT3_COOLDOWN_MS,
    TOP_RESULT_CHANNEL_ID,
    TOP_RESULT_ROLE_ID,
)
from cogs.roles import auto_grant_kit_role
from services.results import ANNOUNCEMENT_FAILED, ANNOUNCEMENT_SENT
from services.tickets import get_ticket, is_ht_fight_ticket
from services.topresult import (
    HT_FIGHT_TIERS,
    format_topresult_message,
    is_registered_kit,
    record_ht_fight,
    set_ht_fight_announcement,
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


class HTFightRetryView(discord.ui.View):
    """Retry odeslání HT Fight oznámení po selhání (záznam zůstal v historii)."""

    def __init__(self, *, result_id, result_channel, content, allowed_mentions):
        super().__init__(timeout=300)
        self.result_id = result_id
        self.result_channel = result_channel
        self.content = content
        self.allowed_mentions = allowed_mentions

    @discord.ui.button(label="🔄 Zkusit odeslat znovu", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro testery.", ephemeral=True
            )
        try:
            sent = await self.result_channel.send(
                content=self.content, allowed_mentions=self.allowed_mentions
            )
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning("Retry HT Fight oznámení selhalo: %s", err)
            return await interaction.response.send_message(
                "❌ Odeslání stále selhává – zkontroluj `TOP_RESULT_CHANNEL_ID`.",
                ephemeral=True,
            )
        await set_ht_fight_announcement(
            self.result_id, ANNOUNCEMENT_SENT, message_id=sent.id
        )
        button.disabled = True
        await interaction.response.edit_message(
            content="✅ Oznámení odesláno.", view=self
        )


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
        fight_tier="HT Fight tier (např. HT3)",
        outcome="Výsledek hráče (vyhrál / prohrál)",
        score="Skóre ve formátu 0-4",
        opponent="Soupeř / tester",
        tier_status="Text stavu tieru (např. Zůstává Low Tier 3)",
        hrac="Testovaný hráč (v HT Fight ticketu se bere z ticketu)",
        ign="Minecraft IGN hráče (v HT Fight ticketu se bere z ticketu)",
        kit="Kit (v HT Fight ticketu se bere z ticketu)",
        bridge="Bridge – povýšení přeskočením na vyšší tier (jen při výhře, např. z LT3 rovnou na LT2)",
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="vyhrál", value="Won"),
            app_commands.Choice(name="prohrál", value="Lost"),
        ]
    )
    @app_commands.autocomplete(
        kit=kit_autocomplete,
        fight_tier=fight_tier_autocomplete,
        bridge=fight_tier_autocomplete,
    )
    async def topresult(
        self,
        interaction: discord.Interaction,
        fight_tier: str,
        outcome: str,
        score: str,
        opponent: discord.User,
        tier_status: str,
        hrac: discord.User | None = None,
        ign: str | None = None,
        kit: str | None = None,
        bridge: str | None = None,
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

        # 0b) Identita hráče: v HT Fight ticketu jsou metadata ticketu
        #     autoritativní (hráč, IGN, kit) a volitelné parametry se ignorují;
        #     mimo ticket musí být hrac/ign/kit zadány.
        ticket = None
        if isinstance(interaction.channel, discord.TextChannel):
            ticket = await get_ticket(interaction.channel_id)

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
            player_id = str(ticket.get("ownerId", ""))
            if hrac is not None and str(hrac.id) != player_id:
                return await interaction.followup.send(
                    "❌ V HT Fight ticketu se zadává výsledek VLASTNÍKA ticketu. "
                    f"Tento ticket patří <@{player_id}> – hráč v příkazu se neshoduje.",
                    ephemeral=True,
                )
            player_name = (
                hrac.display_name or hrac.name if hrac is not None else ""
            ) or ticket.get("ownerName") or ""
            ign_clean = (ticket.get("ign") or "").strip()
            kit_clean = (ticket.get("kit") or "").strip()
        else:
            if hrac is None or not (ign or "").strip() or not (kit or "").strip():
                return await interaction.followup.send(
                    "❌ Mimo HT Fight ticket jsou povinné hrac, IGN a kit.",
                    ephemeral=True,
                )
            player_id = str(hrac.id)
            player_name = hrac.display_name or hrac.name
            ign_clean = (ign or "").strip()
            kit_clean = (kit or "").strip()
            if not is_registered_kit(kit_clean, get_kits()):
                return await interaction.followup.send(
                    f"❌ Neznámý kit `{kit_clean}` – registruj ho přes `/addkit` "
                    "(nebo první `/result`).",
                    ephemeral=True,
                )

        if not ign_clean or not kit_clean:
            return await interaction.followup.send(
                "❌ Ticket nemá IGN/kit hráče – doplň je přes parametry.",
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
        if str(opponent.id) == player_id:
            return await interaction.followup.send(
                "❌ Soupeř nemůže být stejný hráč jako testovaný.", ephemeral=True
            )

        # 1) Zápis do kanonické historie (ht_results.json, resultType=ht_fight):
        #    výhra = povýšení hráče, ticket zůstává otevřený; prohra = zavření
        #    ticketu + HT3+ cooldown vlastníka.
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
            bridge=bridge,
            now=now_ms(),
            date=today_cz(),
            ht3_cooldown_ms=HT3_COOLDOWN_MS,
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
        if r == "identity_conflict":
            return await interaction.followup.send(
                record.get(
                    "message",
                    "❌ Zadané IGN patří jinému hráči (jinému Discord ID) – "
                    "výsledek se nezapsal. Identitu hráče uprav ručně.",
                ),
                ephemeral=True,
            )
        if r in (
            "invalid_argument",
            "invalid_tier",
            "invalid_score",
            "invalid_outcome",
            "invalid_status",
            "invalid_bridge",
        ):
            return await interaction.followup.send(
                record.get("message", "❌ Neplatný výsledek."), ephemeral=True
            )
        if r != "created":
            return await interaction.followup.send(
                "❌ Výsledek se nepodařilo uložit.", ephemeral=True
            )

        rec = record["record"]
        previous_tier = rec.get("previousTier") or ""
        new_tier = rec.get("newTier") or ""
        promoted = bool(new_tier) and new_tier != previous_tier

        # 1a) Výhra = povýšení → udělení nové tier role (stejná centrální
        #     synchronizační logika jako /result, NENÍ to druhý role systém).
        grant_note = ""
        if promoted:
            try:
                grant_note = await auto_grant_kit_role(
                    interaction.guild, player_id, kit_clean, tier_up=new_tier
                )
            except Exception:  # noqa: BLE001
                log.exception("Nelze udělit tier roli po HT Fightu (%s)", kit_clean)
                grant_note = "⚠️ Tier roli se nepodařilo udělit – oprav ji manuálně."

        # 1b) Prohra v HT Fight ticketu = zavřený ticket → obnovíme embed panel.
        if ticket is not None:
            try:
                fresh_ticket = await get_ticket(ticket["id"])
                if fresh_ticket and fresh_ticket.get("panelMessageId"):
                    from views import ticket_embed

                    tch = self.bot.get_channel(int(ticket["id"]))
                    if tch is not None:
                        tmsg = await tch.fetch_message(int(fresh_ticket["panelMessageId"]))
                        await tmsg.edit(embed=ticket_embed(fresh_ticket))
            except Exception:  # noqa: BLE001
                log.exception("Nelze obnovit embed ticketu po /topresult")

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
            previous_tier=previous_tier,
            new_tier=new_tier,
            role_id=TOP_RESULT_ROLE_ID,
        )
        allowed = discord.AllowedMentions(everyone=False, users=True, roles=[target_role])
        announce_result_id = rec.get("id")
        try:
            sent = await result_channel.send(content=message, allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning(
                "Nelze poslat HT Fight výsledek do %s: %s", TOP_RESULT_CHANNEL_ID, err
            )
            try:
                await set_ht_fight_announcement(announce_result_id, ANNOUNCEMENT_FAILED)
            except Exception:  # noqa: BLE001
                log.exception("Nelze označit oznámení jako failed")
            view = HTFightRetryView(
                result_id=announce_result_id,
                result_channel=result_channel,
                content=message,
                allowed_mentions=allowed,
            )
            return await interaction.followup.send(
                f"❌ HT Fight výsledek se nepodařilo odeslat do <#{TOP_RESULT_CHANNEL_ID}> "
                "(záznam zůstal v historii `ht_results.json`, oznámení je ve stavu "
                "**failed**) – můžeš to zkusit znovu tlačítkem.",
                ephemeral=True,
                view=view,
            )
        await set_ht_fight_announcement(
            announce_result_id, ANNOUNCEMENT_SENT, message_id=sent.id
        )

        reply = (
            f"✅ Výsledek HT Fightu (`{ign_clean}` · {kit_clean} · "
            f"**{fight_tier.strip().upper()} Fighty:** {score.strip()}) byl "
            f"zaznamenán a odeslán do <#{TOP_RESULT_CHANNEL_ID}>."
        )
        if promoted:
            reply += f"\n**Povýšení: {previous_tier} → {new_tier}**"
            if rec.get("bridgeTier"):
                reply += " ⚡ (bridge – přeskočení na zadaný tier)"
            if grant_note:
                reply += f"\n{grant_note}"
        await interaction.followup.send(reply, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TopResult(bot))
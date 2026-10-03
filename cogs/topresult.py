"""Cog s HT Fight výsledky – /topresult.

``/topresult`` je specializovaná verze ``/result`` pro HT Fighty – **NENÍ** to
žebříček a nepočítá žádné „top" hráče. Tester v HT Fight ticketu projde
průvodcem (ephemeral): pro každou sekci zápasů vybere soupeře (kdokoliv),
zadá skóre z pohledu hráče a odešle. Výsledek jde veřejně do vyhrazeného
kanálu (``TOP_RESULT_CHANNEL_ID``) s pingem role ``TOP_RESULT_ROLE_ID``:

    <@1419031701920940163> - mendu__ - **Povýšen na LT2** - MolePVP

    **HT3 Fighty (FT4):**
    > vyhrál 4-1 <@1018169843347882076>

    **LT2 Fighty (FT4):**
    > prohrál 2-4 <@1018169843347882077>

    **Postup: HT3 → LT2**

    <@&1523984977371594772>

Pravidla:
- sekce zápasů = tier těsně pod cílovým tierem + cílový tier, u HT3 jen HT3
  (services/ht_fights.py); v každé sekci aspoň 1 soupeř; FT určuje kit
  (``kits.first_to``, admin ho nastaví ``/setkitft``),
- IGN se bere z databáze (``players.ign``), ne z ticketu ani z parametru,
- o povýšení rozhoduje tester parametrem ``tier_ziskan``: při „ano“ hráč
  postoupí na cílový tier (nebo ``bridge``), při „ne“ se HT Fight ticket zavře
  a nastaví se HT3+ cooldown; automatická kontrola výher/passing score není,
  průvodce jen upozorní,
- záznam jde do STEJNÉ kanonické historie jako /result (PostgreSQL ``results``,
  ``kind='ht_fight'``), jeden řádek na zápas, žádná samostatná databáze,
- oznámení má stav pending → sent/failed (retry tlačítkem po selhání),
- příkaz NIKDY nepoužije ``RESULT_CHANNEL_LOWER`` / ``RESULT_CHANNEL_UPPER``,
- idempotence = ticket + tier sekce + soupeř,
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
from cogs.roles import TierRoleGrant, auto_grant_kit_role
from db.services.promotion import grant_confirmation
from services.permissions import has_admin_role
from services.kit_catalog import get_kits
from db.repositories.results import ANNOUNCEMENT_FAILED, ANNOUNCEMENT_SENT
from services.ht_fights import (
    MAX_FIGHTS,
    Fight,
    fight_sections,
    format_topresult_fights_message,
    missing_sections,
    parse_fight_score,
)
from services.tickets import get_ticket, is_ht_fight_ticket, next_ticket_tier
from services.topresult import (
    HT_FIGHT_TIERS,
    describe_opponents,
    is_registered_kit,
    load_fight_context,
    record_ht_fights,
    set_ht_fight_announcement,
    validate_ht_fight_bridge,
    validate_topresult_config,
)
from utils import has_tester_role, kit_autocomplete, now_ms, today_cz

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

    def __init__(
        self,
        *,
        result_ids,
        result_channel,
        content,
        allowed_mentions,
        session_factory=None,
    ):
        super().__init__(timeout=300)
        self.result_ids = list(result_ids)
        self.result_channel = result_channel
        self.content = content
        self.allowed_mentions = allowed_mentions
        # Stav oznámení se zapisuje do PostgreSQL – retry nikdy nesahá na JSON.
        self.session_factory = session_factory
        self._sending = False

    @discord.ui.button(label="🔄 Zkusit odeslat znovu", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro testery.", ephemeral=True
            )
        if self._sending:
            return await interaction.response.defer()
        self._sending = True
        try:
            sent = await self.result_channel.send(
                content=self.content, allowed_mentions=self.allowed_mentions
            )
        except (discord.Forbidden, discord.HTTPException) as err:
            self._sending = False
            log.warning("Retry HT Fight oznámení selhalo: %s", err)
            return await interaction.response.send_message(
                "❌ Odeslání stále selhává – zkontroluj `TOP_RESULT_CHANNEL_ID`.",
                ephemeral=True,
            )
        for result_id in self.result_ids:
            await set_ht_fight_announcement(
                result_id,
                ANNOUNCEMENT_SENT,
                message_id=sent.id,
                session_factory=self.session_factory,
            )
        button.disabled = True
        await interaction.response.edit_message(
            content="✅ Oznámení odesláno.", view=self
        )


def _tier_label(tier) -> str:
    return tier or "bez tieru"


class ScoreModal(discord.ui.Modal):
    """Jedno pole se skóre na každého vybraného soupeře (max. 5)."""

    def __init__(self, wizard: "FightWizard"):
        super().__init__(title=f"{wizard.target_tier} {wizard.kit_name} – skóre hráč-soupeř"[:45])
        self.wizard = wizard
        self.fields: list[tuple[str, str, discord.ui.TextInput]] = []
        ft = wizard.first_to
        for tier in wizard.sections:
            for uid in wizard.chosen[tier]:
                info = wizard.opp_info.get(uid, {})
                label = (
                    f"{info.get('ign') or wizard.names.get(uid, uid)} · "
                    f"{_tier_label(info.get('tier'))} · {tier} fight"
                )[:45]
                field = discord.ui.TextInput(
                    label=label,
                    placeholder=f"FT{ft} — např. {ft}-1 · vzdal: {ft}-1 ff"[:100],
                    default=(
                        wizard.scores[(tier, uid)].text
                        if (tier, uid) in wizard.scores
                        else None
                    ),
                    max_length=20,
                )
                self.add_item(field)
                self.fields.append((tier, uid, field))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wizard = self.wizard
        errors = []
        parsed = {}
        for tier, uid, field in self.fields:
            score, error = parse_fight_score(field.value, wizard.first_to)
            if score is None:
                errors.append(
                    f"• **{wizard.names.get(uid, uid)}** ({tier}): {error}"
                )
            else:
                parsed[(tier, uid)] = score
        if errors:
            return await interaction.response.send_message(
                "❌ Skóre se neuložilo:\n" + "\n".join(errors), ephemeral=True
            )
        wizard.scores.update(parsed)
        wizard.sync_components()
        await interaction.response.edit_message(embed=wizard.embed(), view=wizard)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Chyba v modálu /topresult: %s", error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class FightWizard(discord.ui.View):
    """Ephemeral průvodce: výběr soupeřů po sekcích → skóre → náhled → odeslání."""

    def __init__(
        self,
        cog: "TopResult",
        *,
        evaluator,
        player_id: str,
        player_name: str,
        ign: str,
        kit_name: str,
        first_to: int,
        current_tier,
        target_tier: str,
        tier_gained: bool,
        bridge,
        ticket,
        sections: list[str],
        result_channel,
        target_role,
        guild,
        session_factory,
    ):
        super().__init__(timeout=900)
        self.cog = cog
        self.evaluator = evaluator
        self.player_id = player_id
        self.player_name = player_name
        self.ign = ign
        self.kit_name = kit_name
        self.first_to = first_to
        self.current_tier = current_tier
        self.target_tier = target_tier
        self.tier_gained = tier_gained
        self.bridge = bridge
        self.ticket = ticket
        self.sections = sections
        self.result_channel = result_channel
        self.target_role = target_role
        self.guild = guild
        self.session_factory = session_factory

        self.chosen: dict[str, list[str]] = {tier: [] for tier in sections}
        self.names: dict[str, str] = {}
        self.opp_info: dict[str, dict] = {}
        self.scores: dict[tuple[str, str], object] = {}
        self._submitting = False
        self.selects: dict[str, discord.ui.UserSelect] = {}

        for row, tier in enumerate(sections):
            select = discord.ui.UserSelect(
                placeholder=f"{tier} Soupeři — s kým hrál?",
                min_values=0,
                max_values=MAX_FIGHTS,
                row=row,
            )
            select.callback = self._make_select_callback(tier, select)
            self.selects[tier] = select
            self.add_item(select)

        button_row = len(sections)
        self.score_button = discord.ui.Button(
            label="Zadat skóre", emoji="✏️", style=discord.ButtonStyle.primary, row=button_row
        )
        self.score_button.callback = self.on_score
        self.preview_button = discord.ui.Button(
            label="Náhled", emoji="👁️", style=discord.ButtonStyle.success, row=button_row
        )
        self.preview_button.callback = self.on_preview
        self.send_button = discord.ui.Button(
            label="Odeslat", emoji="📨", style=discord.ButtonStyle.success, row=button_row
        )
        self.send_button.callback = self.on_send
        cancel = discord.ui.Button(
            label="Zrušit", emoji="❌", style=discord.ButtonStyle.danger, row=button_row
        )
        cancel.callback = self.on_cancel
        for item in (self.score_button, self.preview_button, self.send_button, cancel):
            self.add_item(item)
        self.sync_components()

    # ---- stav -----------------------------------------------------------
    def total_selected(self) -> int:
        return sum(len(ids) for ids in self.chosen.values())

    def ready_for_scores(self) -> bool:
        return not missing_sections(self.sections, self.chosen)

    def complete(self) -> bool:
        return self.ready_for_scores() and all(
            (tier, uid) in self.scores
            for tier in self.sections
            for uid in self.chosen[tier]
        )

    def sync_components(self) -> None:
        for tier, select in self.selects.items():
            select.default_values = [
                discord.SelectDefaultValue(
                    id=int(uid), type=discord.SelectDefaultValueType.user
                )
                for uid in self.chosen[tier]
            ]
        self.score_button.disabled = not self.ready_for_scores()
        self.send_button.disabled = not self.complete()

    def fights(self) -> list[Fight]:
        return [
            Fight(
                tier=tier,
                opponent_id=uid,
                opponent_name=self.names.get(uid, uid),
                score=self.scores[(tier, uid)],
            )
            for tier in self.sections
            for uid in self.chosen[tier]
        ]

    def goal_tier(self) -> str:
        return (self.bridge or self.target_tier) if self.tier_gained else ""

    def status_text(self) -> str:
        goal = self.goal_tier()
        if goal:
            return f"Povýšen na {goal}"
        return f"Zůstává {self.current_tier}" if self.current_tier else "Beze změny"

    def preview_text(self) -> str:
        goal = self.goal_tier()
        return format_topresult_fights_message(
            player_id=self.player_id,
            ign=self.ign,
            tier_status=self.status_text(),
            kit=self.kit_name,
            first_to=self.first_to,
            fights=self.fights(),
            previous_tier=self.current_tier or "N/A",
            new_tier=goal,
            role_id=TOP_RESULT_ROLE_ID,
        )

    def warnings(self) -> list[str]:
        out = []
        lower = self.sections[0] if len(self.sections) == 2 else None
        if lower and self.complete():
            if not any(
                self.scores[(lower, uid)].outcome == "Won" for uid in self.chosen[lower]
            ):
                out.append(f"⚠️ V sekci **{lower}** není žádná výhra.")
        return out

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🏆 {self.target_tier} test · {self.kit_name}",
            description=(
                f"👤 <@{self.player_id}> · {self.ign} · teď "
                f"**{_tier_label(self.current_tier)}**\n"
                f"Tier získává: **{'ano' if self.tier_gained else 'ne'}**"
            ),
            colour=discord.Colour.gold(),
        )
        for tier in self.sections:
            lines = []
            for uid in self.chosen[tier]:
                info = self.opp_info.get(uid, {})
                line = f"<@{uid}> · {info.get('ign') or '?'} · {_tier_label(info.get('tier'))}"
                score = self.scores.get((tier, uid))
                if score is not None:
                    line += f" — **{score.text}**"
                lines.append(line)
            embed.add_field(
                name=f"{tier} Soupeři · FT{self.first_to}",
                value="\n".join(lines) if lines else "*nikdo*",
                inline=False,
            )
        if not self.ready_for_scores():
            hint = "👉 V menu níže vyber, s kým hráč hrál (v každé sekci aspoň jednoho)."
        elif not self.complete():
            hint = "👉 Zadej skóre tlačítkem ✏️ – vždy hráč-soupeř, např. `4-1`."
        else:
            hint = "👉 Zkontroluj náhled a odešli výsledek tlačítkem 📨."
        embed.add_field(name="​", value=hint, inline=False)
        return embed

    # ---- komponenty -----------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.evaluator.id:
            await interaction.response.send_message(
                "❌ Tento průvodce patří jinému testerovi.", ephemeral=True
            )
            return False
        return True

    def _make_select_callback(self, tier: str, select: discord.ui.UserSelect):
        async def callback(interaction: discord.Interaction) -> None:
            users = list(select.values)
            problem = None
            if any(getattr(u, "bot", False) for u in users):
                problem = "❌ Soupeř nemůže být bot."
            elif any(str(u.id) == self.player_id for u in users):
                problem = "❌ Soupeř nemůže být stejný hráč jako testovaný."
            elif (
                self.total_selected() - len(self.chosen[tier]) + len(users) > MAX_FIGHTS
            ):
                problem = f"❌ Dohromady nejvýš {MAX_FIGHTS} soupeřů (limit formuláře Discordu)."
            if problem is None:
                self.chosen[tier] = [str(u.id) for u in users]
                for u in users:
                    self.names[str(u.id)] = u.display_name or u.name
                self.scores = {
                    key: value
                    for key, value in self.scores.items()
                    if key[1] in self.chosen[key[0]]
                }
                self.opp_info.update(
                    await describe_opponents(
                        [str(u.id) for u in users],
                        self.kit_name,
                        session_factory=self.session_factory,
                    )
                )
            self.sync_components()
            await interaction.response.edit_message(embed=self.embed(), view=self)
            if problem is not None:
                await interaction.followup.send(problem, ephemeral=True)

        return callback

    async def on_score(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ScoreModal(self))

    async def on_preview(self, interaction: discord.Interaction) -> None:
        if not self.complete():
            return await interaction.response.send_message(
                "❌ Nejdřív vyber soupeře a zadej skóre.", ephemeral=True
            )
        text = self.preview_text()
        if self.warnings():
            text += "\n\n" + "\n".join(self.warnings())
        await interaction.response.send_message(
            f"👁️ **Náhled veřejné zprávy:**\n{text}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="❌ Zrušeno – nic se nezapsalo.", embed=None, view=None
        )

    async def on_send(self, interaction: discord.Interaction) -> None:
        if not self.complete():
            return await interaction.response.send_message(
                "❌ Nejdřív vyber soupeře a zadej skóre.", ephemeral=True
            )
        if self._submitting:
            return await interaction.response.defer()
        self._submitting = True
        await interaction.response.defer(ephemeral=True)
        done = False
        try:
            done = await self.cog.finalize(interaction, self)
        finally:
            self._submitting = False
        if done:
            self.stop()
            try:
                await interaction.edit_original_response(
                    content="✅ Hotovo.", embed=None, view=None
                )
            except (discord.NotFound, discord.HTTPException):
                pass

    async def on_error(self, interaction, error, item) -> None:
        log.exception("Chyba v průvodci /topresult (%s): %s", item, error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.HTTPException, discord.Forbidden):
            pass


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
        tier_ziskan="Získává hráč cílový tier? (ano = povýšení, ne = zavře se HT Fight ticket)",
        hrac="Testovaný hráč (v HT Fight ticketu se bere z ticketu)",
        kit="Kit (v HT Fight ticketu se bere z ticketu)",
        bridge="Bridge – povýšení přeskočením na vyšší tier (jen při „ano“, např. z LT3 rovnou na LT2)",
    )
    @app_commands.choices(
        tier_ziskan=[
            app_commands.Choice(name="ano", value="ano"),
            app_commands.Choice(name="ne", value="ne"),
        ]
    )
    @app_commands.autocomplete(kit=kit_autocomplete, bridge=fight_tier_autocomplete)
    async def topresult(
        self,
        interaction: discord.Interaction,
        tier_ziskan: str,
        hrac: discord.User | None = None,
        kit: str | None = None,
        bridge: str | None = None,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        sf = getattr(self.bot, "db_session_factory", None)
        tier_gained = tier_ziskan == "ano"

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

        if bridge:
            if not tier_gained:
                return await interaction.followup.send(
                    "❌ Bridge se zadává jen při „ano“ – při „ne“ hráč nepostupuje.",
                    ephemeral=True,
                )
            ok, msg = validate_ht_fight_bridge(bridge)
            if not ok:
                return await interaction.followup.send(msg, ephemeral=True)

        # 0b) Identita hráče: v HT Fight ticketu jsou metadata ticketu
        #     autoritativní (hráč, kit, cílový tier); mimo ticket musí být
        #     hrac a kit zadány. IGN se vždy bere z databáze.
        ticket = None
        if isinstance(interaction.channel, discord.TextChannel):
            ticket = await get_ticket(interaction.channel_id, session_factory=sf)

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
            kit_clean = (ticket.get("kit") or "").strip()
        else:
            if hrac is None or not (kit or "").strip():
                return await interaction.followup.send(
                    "❌ Mimo HT Fight ticket jsou povinné hrac a kit.", ephemeral=True
                )
            player_id = str(hrac.id)
            player_name = hrac.display_name or hrac.name
            kit_clean = (kit or "").strip()
            if not is_registered_kit(kit_clean, await get_kits(session_factory=sf)):
                return await interaction.followup.send(
                    f"❌ Neznámý kit `{kit_clean}` – registruj ho přes `/addkit` "
                    "(nebo první `/result`).",
                    ephemeral=True,
                )

        if not kit_clean:
            return await interaction.followup.send(
                "❌ Ticket nemá kit – doplň ho přes parametr.", ephemeral=True
            )
        if player_id == str(interaction.user.id) and not has_admin_role(interaction.user):
            return await interaction.followup.send(
                "❌ Nemůžeš zapisovat výsledek sám sobě.", ephemeral=True
            )

        # 0c) Kontext z databáze: IGN, aktuální tier, FT kitu.
        context = await load_fight_context(player_id, kit_clean, session_factory=sf)
        if context is None:
            return await interaction.followup.send(
                f"❌ Neznámý kit `{kit_clean}`.", ephemeral=True
            )
        if not context["ign"]:
            return await interaction.followup.send(
                f"❌ Hráč <@{player_id}> nemá v databázi IGN – nejdřív `/linkign` "
                "(nebo admin `/linkdiscord`).",
                ephemeral=True,
            )
        if not context["first_to"]:
            return await interaction.followup.send(
                f"❌ Kit **{context['kit_name']}** nemá nastavené FT – admin ho nastaví "
                "přes `/setkitft`.",
                ephemeral=True,
            )

        # 0d) Cílový tier: z ticketu, jinak o stupeň nad aktuálním tierem.
        current_tier = context["current_tier"]
        target_tier = ((ticket or {}).get("targetTier") or "").strip().upper()
        if not target_tier and current_tier:
            target_tier = next_ticket_tier(current_tier) or ""
        sections = fight_sections(target_tier) if target_tier else []
        if not sections:
            return await interaction.followup.send(
                "❌ Nepodařilo se určit cílový tier testu (hráč nemá tier a ticket "
                "nemá cíl).",
                ephemeral=True,
            )

        wizard = FightWizard(
            self,
            evaluator=interaction.user,
            player_id=player_id,
            player_name=player_name,
            ign=context["ign"],
            kit_name=context["kit_name"],
            first_to=context["first_to"],
            current_tier=current_tier,
            target_tier=target_tier,
            tier_gained=tier_gained,
            bridge=(bridge or "").strip().upper() or None,
            ticket=ticket,
            sections=sections,
            result_channel=result_channel,
            target_role=target_role,
            guild=interaction.guild,
            session_factory=sf,
        )
        await interaction.followup.send(embed=wizard.embed(), view=wizard, ephemeral=True)

    # ------------------------------------------------------------------
    # Odeslání výsledku z průvodce
    # ------------------------------------------------------------------
    async def finalize(self, interaction: discord.Interaction, wizard: FightWizard) -> bool:
        """Zapíše zápasy, udělí roli, oznámí. ``True`` = hotovo (průvodce se zavře)."""
        sf = wizard.session_factory
        player_id = wizard.player_id
        kit_clean = wizard.kit_name
        ticket = wizard.ticket
        fights = wizard.fights()

        # 1) Zápis do kanonické historie – výhradně PostgreSQL (Result,
        #    promotion_status=discord_pending). „ano“ = povýšení, ticket zůstává
        #    otevřený; „ne“ = zavření ticketu + HT3+ cooldown vlastníka.
        record = await record_ht_fights(
            ticket_id=ticket["id"] if ticket is not None else None,
            player_id=player_id,
            player_name=wizard.player_name,
            evaluator_id=str(interaction.user.id),
            evaluator_name=interaction.user.display_name,
            kit=kit_clean,
            fights=fights,
            target_tier=wizard.target_tier,
            tier_gained=wizard.tier_gained,
            bridge=wizard.bridge,
            now=now_ms(),
            date=today_cz(),
            ht3_cooldown_ms=HT3_COOLDOWN_MS,
            session_factory=sf,
        )
        r = record["result"]
        if r == "duplicate":
            existing = record.get("existing") or {}
            await interaction.followup.send(
                "❌ Tento HT Fight výsledek už byl zaznamenán (idempotentně – nic se "
                f"nemění; skóre {existing.get('score') or '?'} "
                f"dne {existing.get('date') or '?'}).",
                ephemeral=True,
            )
            return False
        errors = {
            "not_found": "❌ Tento kanál není HT ticket.",
            "not_fight_ticket": (
                "❌ Kanál je HT ticket, ale není typu „fight“ – HT Fight výsledky "
                "se zadávají mimo eval tickety."
            ),
            "ticket_closed": "❌ HT Fight ticket je zavřený.",
            "ign_missing": (
                f"❌ Hráč <@{player_id}> nemá v databázi IGN – nejdřív `/linkign`."
            ),
        }
        if r in errors:
            await interaction.followup.send(errors[r], ephemeral=True)
            return False
        if r == "wrong_player":
            owner = (record.get("ticket") or {}).get("ownerId")
            await interaction.followup.send(
                "❌ Ticket patří jinému hráči "
                f"({f'<@{owner}>' if owner else 'neznámý'}).",
                ephemeral=True,
            )
            return False
        if r == "wrong_kit":
            kit_of = (record.get("ticket") or {}).get("kit")
            await interaction.followup.send(
                f"❌ Kit neodpovídá ticketu – ticket je na kit **{kit_of}**.",
                ephemeral=True,
            )
            return False
        if r in (
            "invalid_argument",
            "invalid_tier",
            "invalid_bridge",
        ):
            await interaction.followup.send(
                record.get("message", "❌ Neplatný výsledek."), ephemeral=True
            )
            return False
        if r != "created":
            await interaction.followup.send("❌ Výsledek se nepodařilo uložit.", ephemeral=True)
            return False

        rec = record["record"]
        ign_clean = record["ign"]
        records = record["records"]
        result_ids = [item.get("id") for item in records]
        previous_tier = rec.get("previousTier") or ""
        new_tier = rec.get("newTier") or ""
        promoted = bool(new_tier) and new_tier != previous_tier

        # 1a) Výhra = povýšení → udělení nové tier role (stejná centrální
        #     synchronizační logika jako /result, NENÍ to druhý role systém).
        grant_note = ""
        grant = None
        if promoted:
            try:
                grant = await auto_grant_kit_role(
                    interaction.guild,
                    player_id,
                    kit_clean,
                    tier_up=new_tier,
                    session_factory=sf,
                )
                grant_note = (
                    grant.note if isinstance(grant, TierRoleGrant) else grant
                )
            except Exception:  # noqa: BLE001
                log.exception("Nelze udělit tier roli po HT Fightu (%s)", kit_clean)
                grant_note = "⚠️ Tier roli se nepodařilo udělit – oprav ji manuálně."

        # 1a2) PostgreSQL mirror (Discord-first, invariant 6): JEDINÝ kanonický
        #      zápis povýšení (``db.services.commit_confirmed_promotion``) –
        #      nepotvrzený/nejistý Discord grant se odmítne v něm, nic se
        #      nezapíše; selže-li transakce, událost jde do outboxu.
        db_note = ""
        if promoted:
            try:
                from db.services import commit_confirmed_promotion

                wedge = await commit_confirmed_promotion(
                    sf,
                    grant=grant,
                    result_key=f"ht_fight:{rec.get('id')}",
                    kind="ht_fight",
                    discord_id=int(player_id),
                    ign=ign_clean,
                    kit_key=kit_clean,
                    new_tier_code=new_tier,
                    previous_tier_code=(
                        previous_tier if previous_tier not in ("N/A", "") else None
                    ),
                    bridge_tier_code=(rec.get("bridgeTier") or None),
                    tier_status=(rec.get("tierStatus") or wizard.status_text()),
                    score=(rec.get("score") or "").strip(),
                    outcome=rec.get("outcome") or "",
                    opponent_id=(
                        int(rec["opponentId"]) if rec.get("opponentId") else None
                    ),
                    opponent_name=(rec.get("opponentName") or None),
                    date=(rec.get("date") or None),
                    audit_actor_id=interaction.user.id,
                    audit_actor_name=str(interaction.user),
                )
                if wedge.message:
                    db_note = f"\n{wedge.message}"
            except Exception:  # noqa: BLE001 – mirror nesmí zablokovat /topresult
                log.exception("PostgreSQL mirror pro HT Fight %s selhal", kit_clean)

            if not grant_confirmation(grant)[0]:
                await interaction.followup.send(
                    "❌ Povýšení **nebylo potvrzeno** – tier roli se nepodařilo udělit "
                    "a ověřit, proto se nic neoznámilo ani nezapsalo do mirroru. "
                    "Oprav role a zapiš `/topresult` znovu."
                    + (f"\n{grant_note}" if grant_note else "")
                    + db_note,
                    ephemeral=True,
                )
                return False

        # 1b) „Tier nezískán“ v HT Fight ticketu = zavřený ticket → obnovíme embed panel.
        if ticket is not None:
            try:
                fresh_ticket = await get_ticket(ticket["id"], session_factory=sf)
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
        message = format_topresult_fights_message(
            player_id=player_id,
            ign=ign_clean,
            tier_status=rec.get("tierStatus") or wizard.status_text(),
            kit=kit_clean,
            first_to=wizard.first_to,
            fights=fights,
            previous_tier=previous_tier,
            new_tier=new_tier,
            role_id=TOP_RESULT_ROLE_ID,
        )
        mention_ids = {int(player_id), *(int(f.opponent_id) for f in fights)}
        allowed = discord.AllowedMentions(
            everyone=False,
            users=[discord.Object(uid) for uid in mention_ids],
            roles=[wizard.target_role],
        )
        try:
            sent = await wizard.result_channel.send(content=message, allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as err:
            log.warning(
                "Nelze poslat HT Fight výsledek do %s: %s", TOP_RESULT_CHANNEL_ID, err
            )
            try:
                for result_id in result_ids:
                    await set_ht_fight_announcement(
                        result_id, ANNOUNCEMENT_FAILED, session_factory=sf
                    )
            except Exception:  # noqa: BLE001
                log.exception("Nelze označit oznámení jako failed")
            view = HTFightRetryView(
                result_ids=result_ids,
                result_channel=wizard.result_channel,
                content=message,
                allowed_mentions=allowed,
                session_factory=sf,
            )
            await interaction.followup.send(
                f"❌ HT Fight výsledek se nepodařilo odeslat do <#{TOP_RESULT_CHANNEL_ID}> "
                "(záznam zůstal v historii PostgreSQL, oznámení je ve stavu "
                "**failed**) – můžeš to zkusit znovu tlačítkem.",
                ephemeral=True,
                view=view,
            )
            return True
        for result_id in result_ids:
            await set_ht_fight_announcement(
                result_id,
                ANNOUNCEMENT_SENT,
                message_id=sent.id,
                session_factory=sf,
            )

        reply = (
            f"✅ Výsledek HT Fightu (`{ign_clean}` · {kit_clean} · {len(fights)} "
            f"zápas(ů)) byl zaznamenán a odeslán do <#{TOP_RESULT_CHANNEL_ID}>."
        )
        if promoted:
            reply += f"\n**Povýšení: {previous_tier} → {new_tier}**"
            if rec.get("bridgeTier"):
                reply += " ⚡ (bridge – přeskočení na zadaný tier)"
            if grant_note:
                reply += f"\n{grant_note}"
            if db_note:
                reply += f"\n{db_note}"
        elif not wizard.tier_gained and ticket is not None:
            reply += "\n🔒 Tier nezískán – HT Fight ticket byl zavřen a nastaven cooldown."
        await interaction.followup.send(reply, ephemeral=True)
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TopResult(bot))

"""Cog s výsledky tier testů a statistikami testerů.

- /result            – zápis výsledku testu (cooldown, `results`, role)
- /testerstats       – portfolio jednoho testera
- /testersstats      – tabulka testerů (tento měsíc / všechny časy)
- /addtest           – admin: přidání historických testů (do `tester_credits`)
- /removetest        – admin: odečtení testů (upraví total i aktuální měsíc, min 0)
- /removeplayertiers – tiery v PostgreSQL režimu nelze mazat; píše, jak to udělat

Statistiky testerů se nepočítají do žádného JSONu: odvozují se za běhu z
tabulky `results` (viz `services.tester_stats`), ruční kredity z `/addtest`
leží v `tester_credits`. Zápis na GitHub/web je oddělený projekce přes
`/websync`, nikoli součást `/result`.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    HT3_COOLDOWN_MS,
    PLAYER_COOLDOWN_MS,
    get_result_channel_id,
)
from panel import update_panel
from services.permissions import has_admin_role
from services.queue_service import leave_queue, remove_pulled_player
from services.results import (
    EVAL_TIER,
    RESULT_TIERS,
    record_result,
    validate_result_tier,
)
from services.tester_stats import (
    credit_tester,
    remove_tester_credit,
    tester_leaderboard,
    tester_stats,
)
from services.evals import set_eval
from services.kit_catalog import add_kit, get_kits
from services.tickets import HT3_TIER_LADDER, get_ticket
from utils import (
    has_tester_role,
    kit_autocomplete,
    now_ms,
    today_cz,
)

from cogs.roles import TierRoleGrant, auto_grant_kit_role

log = logging.getLogger("dachshundtiers")

# Povolené tiery v /result a EVAL_TIER žijí v services/results.py (jediný
# zdroj pravdy): queue výsledky LT5..LT3+eval, HT3+ ticket výsledky ověřuje
# validate_result_tier přímo proti žebříčku (HT3 a výš = jen přes tickety).

# Autocomplete tieru pro /result: (zobrazované jméno, přenášená hodnota).
# „LT3 + eval" se přenáší jako LT3E (= EVAL_TIER), ať zůstane normalizace
# v /result stejná (tier.strip().upper()).
TIER_OPTIONS = [
    ("LT5", "LT5"),
    ("HT5", "HT5"),
    ("LT4", "LT4"),
    ("HT4", "HT4"),
    ("LT3", "LT3"),
    ("LT3 + eval", EVAL_TIER),
]


async def tier_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice]:
    """Autocomplete tierů /result (LT5 .. LT3 + eval), filtruje podle psaní.

    Když /result běží v kanálu HT3+ ticketu, nabídne navíc tiery žebříčku
    (HT3, LT2, HT2, LT1, HT1) – v ticketu se zapisuje cíl evaluace.
    """
    text = (current or "").lower()
    out = []
    for name, value in TIER_OPTIONS:
        if not text or text in name.lower() or text in value.lower():
            out.append(app_commands.Choice(name=name, value=value))
    ticket = None
    if isinstance(interaction.channel, discord.TextChannel):
        # G0 audit fix: without session_factory this only ever reads the
        # legacy JSON ticket store — in DB mode (where tickets are created
        # via cogs/ht3.py with session_factory) it would always be empty.
        ticket = await get_ticket(
            interaction.channel_id,
            session_factory=getattr(interaction.client, "db_session_factory", None),
        )
    if ticket is not None:
        for t in HT3_TIER_LADDER:
            if t in RESULT_TIERS:  # LT3+eval už je v TIER_OPTIONS
                continue
            if not text or text in t.lower():
                out.append(app_commands.Choice(name=t, value=t))
    return out[:25]


# Statistiky testera se už tady nepočítají. `/result` zapisuje řádek do
# `results` (jednou, transakčně, jako součást `record_result`) a
# `services.tester_stats` je odvozuje za běhu. Dřív to byla JSON agregace
# nad `testers_stats.json`, která se musela hlídat proti souběžnému
# přepisování – to je právě ta třída chyby, kterou odvozování z řádků
# odstraňuje.


class Results(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        """Zachytí neošetřené chyby příkazů – pošle hlášku a zaloguje traceback."""
        log.exception("Chyba v příkazu %s: %s", interaction.command, error)
        msg = "❌ Nastala neočekávaná chyba. Detaily najdeš v logu bota."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # /result
    # ------------------------------------------------------------------
    @app_commands.command(name="result", description="Submit a test result")
    @app_commands.describe(
        hrac="The tested Discord player",
        ign="Minecraft IGN of the player",
        kit="The kit tested",
        tier="New achieved tier",
        score="Score of the match (e.g. 5-2)",
        outcome="Tester outcome",
        notes="Poznámky k testu – uloží se do historie výsledku (nepovinné)",
    )
    @app_commands.choices(
        outcome=[
            app_commands.Choice(name="Tester Won", value="Won"),
            app_commands.Choice(name="Tester Lost", value="Lost"),
        ]
    )
    @app_commands.autocomplete(kit=kit_autocomplete, tier=tier_autocomplete)
    async def result(
        self,
        interaction: discord.Interaction,
        hrac: discord.User,
        ign: str,
        kit: str,
        tier: str,
        score: str,
        outcome: str,
        notes: str = None,
    ) -> None:
        if not has_tester_role(interaction.user):
            return await interaction.response.send_message("❌ Pouze pro testery.", ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        # Self-result: tester si NEMŮŽE zapisovat výsledek sám sobě – tester
        # hraje PROTI hráči, který je testovaný. Admini (jiný subjekt dohledu)
        # můžou zapsat i sobě, tester ne.
        if hrac.id == interaction.user.id and not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Nemůžeš zapisovat výsledek sám sobě.", ephemeral=True
            )

        # Défer hned napřed – stejně jako originál (index.ts: deferReply),
        # abychom se vešli do 3s okna Discord i přes fetch roomky / kanálu.
        await interaction.response.defer(ephemeral=True)

        target_id = str(hrac.id)
        ign_clean = ign.strip()
        kit_clean = kit.strip()
        kit_key = kit_clean.lower()
        tier_up = tier.strip().upper()

        notes_clean = (notes or "").strip() or None

        # G0 audit fix (H1 cutover): resolved once, used for every
        # dual-mode call below — ticket lookup, kit registration, the
        # canonical record_result write, queue/room cleanup. Previously only
        # some of these calls received it; record_result and get_ticket
        # never did, so /result always used the legacy JSON path for the
        # promotion decision even when PostgreSQL was fully configured.
        sf = getattr(self.bot, "db_session_factory", None)

        # 0) HT ticket jako počátek evaluace: když /result běží v kanálu
        #    HT3+ ticketu, výsledek se propojí s ticketem (idempotence:
        #    1 ticket = max. 1 výsledek) a ticket se po potvrzení zavře.
        #    Mimo ticket jde o klasický queue výsledek.
        ticket = None
        if isinstance(interaction.channel, discord.TextChannel):
            ticket = await get_ticket(interaction.channel_id, session_factory=sf)

        # 0a) Validace tieru:
        #     - v HT ticketu: tier ze žebříčku, maximálně cíl ticketu a
        #       nikdy horší než aktuální tier hráče (retest nedegraduje),
        #     - mimo ticket: jen LT5..LT3+eval (HT3 a výš se řeší výhradně
        #       přes HT3+ tickety).
        ok_tier, tier_msg = validate_result_tier(
            tier_up,
            target_tier=ticket.get("targetTier") if ticket is not None else None,
            current_tier=ticket.get("currentTier") if ticket is not None else None,
        )
        if not ok_tier:
            return await interaction.followup.send(tier_msg, ephemeral=True)

        # 0b) „LT3 + eval" (LT3E) = tier LT3 (stejná role) + eval status.
        #     Do webu (export) / role jde „LT3", eval se uloží zvlášť.
        is_eval = tier_up == EVAL_TIER
        stored_tier = "LT3" if is_eval else tier_up  # web export + role
        display_tier = "LT3 + eval" if is_eval else tier_up  # embed / texty

        # 0) Auto-registrace nového kitu (jako /addkit) – aby se hned objevil
        #    v autocomplete /result, HT3+ panelu a u turnajů.
        new_kit_added = False
        registered = await get_kits(
            session_factory=getattr(self.bot, "db_session_factory", None)
        )
        if not any(existing.lower() == kit_key for existing in registered):
            new_kit_added = await add_kit(
                kit_clean,
                session_factory=getattr(self.bot, "db_session_factory", None),
            )
            if new_kit_added:
                try:
                    from cogs.kits import _refresh_ht3_panel

                    await _refresh_ht3_panel(self.bot)
                except Exception:  # noqa: BLE001
                    pass

        # 1) Zápis výsledku – atomicky: validace + idempotence + historie.
        #    PostgreSQL je JEDINÝ režim (Result, promotion_status=
        #    discord_pending) – players.json se NEpíše a nerozhoduje o current
        #    tieru (G0 audit fix / H1 cutover). Bez session_factory
        #    record_result odmítne (RuntimeError), žádný JSON fallback.
        #    Viz services/results.record_result.
        record = await record_result(
            ticket_id=ticket["id"] if ticket is not None else None,
            player_id=target_id,
            player_name=hrac.display_name or hrac.name,
            ign=ign_clean,
            evaluator_id=str(interaction.user.id),
            evaluator_name=interaction.user.display_name,
            kit=kit_clean,
            new_tier=tier_up,
            display_tier=display_tier,
            score=score,
            outcome=outcome,
            notes=notes_clean,
            eval_flag=is_eval,
            now=now_ms(),
            date=today_cz(),
            queue_cooldown_ms=PLAYER_COOLDOWN_MS,
            ht3_cooldown_ms=HT3_COOLDOWN_MS if ticket is not None else 0,
            session_factory=sf,
        )
        r = record["result"]
        if r == "duplicate":
            existing = record.get("existing") or {}
            if ticket is not None:
                return await interaction.followup.send(
                    "ℹ️ Výsledek pro tento ticket už byl zaznamenán "
                    "(idempotentně – nic se nemění):\n"
                    f"- **Nový tier:** {existing.get('displayTier') or existing.get('newTier')}\n"
                    f"- **Kdy:** {existing.get('date') or '?'}\n"
                    f"- **Tester:** <@{existing.get('evaluatorId', 0)}>\n"
                    "Historie běží v PostgreSQL (`results`, propojené s ticketem).",
                    ephemeral=True,
                )
            summary = ""
            if existing:
                summary = (
                    f" (poslední výsledek: "
                    f"{existing.get('displayTier') or existing.get('newTier')}"
                    f" – {existing.get('date') or '?'})"
                )
            return await interaction.followup.send(
                "ℹ️ Hráč má aktivní cooldown (4 dny po posledním /result) – "
                "pravděpodobně duplicitní odeslání. Nic se nezměnilo." + summary,
                ephemeral=True,
            )
        if r == "not_found":
            return await interaction.followup.send(
                "❌ Tento kanál není HT ticket (záznam chybí).", ephemeral=True
            )
        if r == "ticket_closed":
            return await interaction.followup.send(
                "❌ Ticket je zavřený – nejdřív ho otevři přes **Reopen** "
                "a výsledek zapiš znovu.",
                ephemeral=True,
            )
        if r == "wrong_player":
            owner = (record.get("ticket") or {}).get("ownerId")
            return await interaction.followup.send(
                "❌ /result v kanálu ticketu zapisuje výsledek VLASTNÍKA "
                f"ticketu. Tento ticket patří <@{owner}> – hráč v příkazu "
                "se neshoduje.",
                ephemeral=True,
            )
        if r == "wrong_kit":
            kit_of = (record.get("ticket") or {}).get("kit")
            return await interaction.followup.send(
                f"❌ Kit neodpovídá ticketu – ticket je na kit **{kit_of}**.",
                ephemeral=True,
            )
        if r == "invalid_tier":
            return await interaction.followup.send(
                record.get("message", "❌ Neplatný výsledek."), ephemeral=True
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
        if r != "created":
            return await interaction.followup.send(
                "❌ Výsledek se nepodařilo uložit.", ephemeral=True
            )
        previous_tier = record.get("previous_tier", "N/A")

        # 1a) Ticket je zavřený – obnovíme embed panel zprávy (jako Close
        #     tlačítko), aby byla vidět zavřená kartička.
        if ticket is not None:
            try:
                fresh_ticket = await get_ticket(ticket["id"], session_factory=sf)
                if fresh_ticket and fresh_ticket.get("panelMessageId"):
                    from views import ticket_embed

                    tch = self.bot.get_channel(int(ticket["id"]))
                    if tch is not None:
                        tmsg = await tch.fetch_message(
                            int(fresh_ticket["panelMessageId"])
                        )
                        await tmsg.edit(embed=ticket_embed(fresh_ticket))
            except Exception:  # noqa: BLE001
                log.exception("Nelze obnovit embed ticketu po /result")

        # 2) Odebrání z fronty (atomicky v transakci – viz services; bez
        #    session_factory leave_queue odmítne, žádný JSON fallback)
        removed_from_queue = await leave_queue(target_id, kit_key, session_factory=sf)
        if removed_from_queue and interaction.guild is not None:
            await update_panel(interaction.guild, kit_key, session_factory=sf)

        # 3) Odebrání práv z tester roomek – po výsledku hráč nesmí zůstat
        #    v žádné roomce. Pokrývá pull tlačítko / /queue pull (záznam
        #    pulled v DB frontě) i přednastavený přístup přes `/mktesterroom
        #    hrac:` – vždy odstraníme hráčův osobní overwrite ve VŠECH
        #    kanálech serveru.
        await remove_pulled_player(target_id, session_factory=sf)

        member = interaction.guild.get_member(int(target_id))
        if member is None:
            try:
                member = await interaction.guild.fetch_member(int(target_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        if member is not None:
            removed_count = 0
            for ch in interaction.guild.channels:
                try:
                    has_member_ow = any(
                        isinstance(t, discord.Member) and t.id == member.id
                        for t in ch.permission_overwrites
                    )
                except (AttributeError, TypeError):
                    continue
                if not has_member_ow:
                    continue
                try:
                    await ch.set_permissions(member, overwrite=None)
                    removed_count += 1
                except (discord.Forbidden, discord.HTTPException) as err:
                    log.warning(
                        "Nelze odebrat práva hráče %s v kanálu %s: %s",
                        target_id,
                        ch.id,
                        err,
                    )
            if removed_count:
                log.info(
                    "Po /result odebrána práva hráče %s v %d kanálu(ech)",
                    target_id,
                    removed_count,
                )

            # Voice roomky: odebrání práv z voice kanálu hráče automaticky
            # NEodpojí – kdo je zrovna připojený, zůstane viset v roomce.
            # Po výsledku ho proto přesuneme do AFK kanálu (nebo odpojíme).
            try:
                vs = member.voice
            except (AttributeError, TypeError):
                vs = None
            if vs is not None and vs.channel is not None:
                try:
                    await member.move_to(interaction.guild.afk_channel)
                except (discord.Forbidden, discord.HTTPException) as err:
                    log.warning(
                        "Nelze odpojit hráče %s z voice roomky po /result: %s",
                        target_id,
                        err,
                    )

        # 4) Statistiky testera se nepočítají – odvozují se za běhu z tabulky
        #    `results` (viz services.tester_stats). Kdyby se tady agregoval
        #    počet, šel by mimo `record_result` a o souběžné /result by se
        #    ztratil. Datum v patičce embeda drží popisek dne.
        current_date = today_cz()

        # 5) record_result (krok 1) už uložil kanonický záznam do `results`
        #    (řádek `discord_pending`). previous_tier už je vyřešený, tady jen
        #    navazující kroky.

        # 5a) „LT3 + eval" → eval flag. Tier/role zůstávají LT3 – hráč ale
        #     nově může otevírat HT3+ tickety.
        eval_note = ""
        if is_eval and await set_eval(
            ign_clean,
            kit_clean,
            session_factory=getattr(self.bot, "db_session_factory", None),
        ):
            eval_note = (
                f"\n🎖️ **{ign_clean}** dostal **LT3 + eval** pro **{kit_clean}** – "
                "může otevírat HT3+ tickety."
            )

        # 5b) Automatická role kitu+tieru – po uložení výsledku dostane hráč
        #     roli nového tieru, staré tiery kitu se odeberou. Volitelné
        #     `add_role` / `remove_role` byly odebrány: role se odvozují z
        #     tieru podle business logiky a ruční zásah do rolí nebylo
        #     auditovatelné (roli mohl tester přidat komukoli, bez stop, a
        #     nešlo to zjistit později). Změna je tu auditovaná přes
        #     `commit_confirmed_promotion` v kroku 5d + `audit_logs`.
        #     Poznámka se připojí k potvrzení.
        role_note = ""
        grant = None
        try:
            grant = await auto_grant_kit_role(
                interaction.guild,
                target_id,
                kit_key,
                stored_tier,
                session_factory=getattr(self.bot, "db_session_factory", None),
            )
            role_note = (
                grant.note if isinstance(grant, TierRoleGrant) else grant
            )
        except Exception:  # noqa: BLE001
            log.exception("Chyba při automatickém udělování role pro %s", target_id)

        # 5d) PostgreSQL mirror (Discord-first) – JEDINÝ kanonický zápis
        #     povýšení (``db.services.commit_confirmed_promotion``). Tenhle
        #     gate neopakuje: nepotvrzený/nejistý Discord grant se v něm
        #     odmítne sám a NIC se do DB nezapíše (invariant 6). Selže-li
        #     transakce, událost jde do outboxu (promotion_commit,
        #     discord_role_confirmed=True) a mirror se doplní podle Discordu
        #     – nikdy naopak.
        db_note = ""
        try:
            from db.services import commit_confirmed_promotion

            wedge = await commit_confirmed_promotion(
                getattr(self.bot, "db_session_factory", None),
                grant=grant,
                result_key=f"result:{record['record'].get('id')}",
                kind="ticket" if ticket is not None else "queue",
                discord_id=int(target_id),
                ign=ign_clean,
                kit_key=kit_key,
                new_tier_code=stored_tier,
                previous_tier_code=(
                    previous_tier if previous_tier not in ("N/A", "") else None
                ),
                score=score,
                outcome=outcome,
                evaluator_discord_id=interaction.user.id,
                ticket_channel_id=(
                    int(ticket["id"]) if ticket is not None else None
                ),
                notes=notes_clean or None,
                eval_flag=is_eval,
                date=current_date,
                close_ticket_channel_id=(
                    int(ticket["id"]) if ticket is not None else None
                ),
                audit_actor_id=interaction.user.id,
                audit_actor_name=str(interaction.user),
            )
            if wedge.message:
                db_note = f"\n{wedge.message}"
        except Exception:  # noqa: BLE001 – mirror nesmí zablokovat /result
            log.exception("PostgreSQL mirror pro %s selhal", target_id)

        # 6) Embed s výsledkem
        avatar_url = f"https://minotar.net/armor/bust/{ign_clean}/100.png"
        embed = (
            discord.Embed(
                title=f"📝 Výsledek tier testu – {kit_clean.lower()}",
                description=(
                    "Evaluace HT3+ ticketu byla úspěšně dokončena!"
                    if ticket is not None
                    else f"Tier test byl úspěšně dokončen pro frontu **{kit_clean.lower()}**!"
                ),
                color=0x10B981,
            )
            .set_thumbnail(url=avatar_url)
            .add_field(name="👤 Hráč (Discord)", value=f"<@{target_id}>", inline=True)
            .add_field(name="🎮 Minecraft IGN", value=f"`{ign_clean}`", inline=True)
            .add_field(name="⚔️ Tester", value=f"<@{interaction.user.id}>", inline=True)
            .add_field(
                name="📊 Skóre / Výsledek",
                value=f"`{score}` ({'Tester vyhrál' if outcome == 'Won' else 'Tester prohrál'})",
                inline=False,
            )
            .add_field(name="📉 Předchozí tier", value=f"`{previous_tier}`", inline=True)
            .add_field(name="📈 Nový tier", value=f"**{display_tier}**", inline=True)
        )
        if ticket is not None:
            embed.add_field(name="🎫 Ticket", value=f"<#{ticket['id']}>", inline=True)
        if notes_clean:
            embed.add_field(name="📝 Poznámky", value=notes_clean, inline=False)
        embed.set_footer(text=current_date)

        # 7) Odeslání výsledku do určeného kanálu podle tieru (jako v originále)
        result_channel_id = get_result_channel_id(stored_tier)
        result_channel = self.bot.get_channel(result_channel_id)
        if result_channel is None and interaction.guild is not None:
            try:
                result_channel = await interaction.guild.fetch_channel(result_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                result_channel = None

        content = (
            f"📢 **Nový výsledek pro {kit_clean}!** | "
            f"Hráč: <@{target_id}> | Tester: <@{interaction.user.id}>"
        )
        saved_msg = (
            f"✅ Výsledek uložen do PostgreSQL pro hráče **{ign_clean}** — "
            f"mód **{kit_clean}**, tier **{display_tier}**."
            "\n🌐 Web se aktualizuje samostatně přes **/websync** "
            "(PostgreSQL → players.json export → GitHub) – /result už na "
            "GitHub neposílá."
        )
        if new_kit_added:
            saved_msg += (
                f"\n🎉 Nový kit **{kit_clean}** byl automaticky zaregistrován "
                "do databáze (`kits` – autocomplete, HT3+ panel, turnaje)."
            )
        if eval_note:
            saved_msg += eval_note
        if role_note:
            saved_msg += role_note
        if db_note:
            saved_msg += db_note
        if ticket is not None:
            saved_msg += (
                "\n🔒 Ticket byl zavřený – hráč má 7denní HT3+ cooldown na "
                "tento kit. Výsledek je propojený s ticketem "
                "(historie v PostgreSQL)."
            )

        if result_channel is not None:
            try:
                await result_channel.send(content=content, embed=embed)
                await interaction.followup.send(
                    f"{saved_msg} Odesláno do <#{result_channel_id}>.",
                    ephemeral=True,
                )
            except (discord.Forbidden, discord.HTTPException) as err:
                log.warning("Nelze poslat do výsledkového kanálu %s: %s", result_channel_id, err)
                await interaction.followup.send(
                    saved_msg, embed=embed, ephemeral=True
                )
        else:
            await interaction.followup.send(
                f"{saved_msg} (Výsledkový kanál <#{result_channel_id}> nebyl nalezen.)",
                embed=embed,
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /testerstats
    # ------------------------------------------------------------------
    @app_commands.command(name="testerstats", description="View detailed stats for a specific tester")
    @app_commands.describe(tester="Select the tester")
    async def testerstats(self, interaction: discord.Interaction, tester: discord.User = None) -> None:
        target = tester or interaction.user
        session_factory = getattr(self.bot, "db_session_factory", None)
        tdata = await tester_stats(str(target.id), session_factory=session_factory)

        if not tdata or tdata.get("total", 0) == 0:
            return await interaction.response.send_message(
                f"❌ Uživatel <@{target.id}> nemá žádné uložené statistiky testů."
            )

        kits = tdata.get("kits", {})
        tiers = tdata.get("tiers", {})
        fav_kit = max(kits, key=kits.get) if kits else "Žádný"
        top_tier = max(tiers, key=tiers.get) if tiers else "Žádný"

        hourly = tdata.get("hourlyLogs", [])
        avg_hour = round(sum(hourly) / len(hourly)) if hourly else 0

        embed = (
            discord.Embed(
                title=f"⚔️ Portfolio Testera – {target.name}",
                color=0x3B82F6,
                timestamp=discord.utils.utcnow(),
            )
            .add_field(name="📈 Celkem testů", value=f"`{tdata.get('total', 0)}`", inline=True)
            .add_field(
                name="📅 Naposledy testoval",
                value=f"`{tdata.get('lastTested') or 'Nikdy'}`",
                inline=True,
            )
            .add_field(name="🎮 Nejoblíbenější Kit", value=f"`{fav_kit}`", inline=True)
            .add_field(name="🏆 Nejčastěji dávaný Tier", value=f"`{top_tier}`", inline=True)
            .add_field(name="⏰ Průměrný čas testu", value=f"`Kolem {avg_hour}:00 hod`", inline=True)
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /testersstats
    # ------------------------------------------------------------------
    @app_commands.command(name="testersstats", description="Zobrazí tabulku testerů.")
    @app_commands.describe(period="Filtruj podle měsíce nebo celkově")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Tento měsíc", value="current"),
            app_commands.Choice(name="Všechny časy", value="all"),
        ]
    )
    async def testersstats(self, interaction: discord.Interaction, period: str) -> None:
        session_factory = getattr(self.bot, "db_session_factory", None)
        entries = await tester_leaderboard(period, session_factory=session_factory)
        top10 = entries[:10]

        if top10:
            description = "\n".join(
                f"**{i + 1}.** <@{tester_id}>: {score} testů"
                for i, (tester_id, score) in enumerate(top10)
            )
        else:
            description = "Žádné testy pro toto období."

        embed = discord.Embed(
            title=(
                "🏆 TOP testeři všechny časy"
                if period == "all"
                else "🏆 TOP testeři tento měsíc"
            ),
            color=0x9B59B6,
            description=description,
        )
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /addtest (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="addtest", description="Admin command to manually add historical test logs")
    @app_commands.describe(
        tester="The tester to credit",
        amount="Amount of tests to add",
        month="Month format (MM.YYYY, e.g. 06.2026)",
    )
    async def addtest(
        self, interaction: discord.Interaction, tester: discord.User, amount: int, month: str
    ) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        tester_id = str(tester.id)
        await credit_tester(
            tester_id,
            amount,
            month,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )

        await interaction.response.send_message(
            f"✅ Úspěšně přidáno **{amount}** historických testů uživateli "
            f"<@{tester_id}> na měsíc **{month}**."
        )

    # ------------------------------------------------------------------
    # /removetest (admin)
    # ------------------------------------------------------------------
    @app_commands.command(name="removetest", description="Odečte testy testerovi")
    @app_commands.describe(
        user="Tester, kterému chceš odebrat testy", amount="Počet testů k odebrání"
    )
    async def removetest(self, interaction: discord.Interaction, user: discord.User, amount: int) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        tester_id = str(user.id)
        updated_total = await remove_tester_credit(
            tester_id,
            amount,
            session_factory=getattr(self.bot, "db_session_factory", None),
        )

        await interaction.response.send_message(
            f"📉 Uživatel **{user.name}** ztratil **{amount}** test(ů). "
            f"Nyní má celkem **{updated_total}** testů."
        )

    # ------------------------------------------------------------------
    # /removeplayertiers (admin)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="removeplayertiers",
        description="Odebere hráči všechny aktuální tiery (historie zůstává)",
    )
    @app_commands.describe(ign="Minecraft jméno hráče (IGN)")
    async def removeplayertiers(self, interaction: discord.Interaction, ign: str) -> None:
        """V PostgreSQL režimu tiery mazat nejde — a nejde to omylem, ne chybou.

        `player_current_tiers` je zrcadlo toho, co potvrzuje Discord, a zrcadlo
        nemá „clear" operaci: chybějící role je anomálie, kterou má odhalit
        `/sync check`, ne tichý výmaz. Dřív tu byla JSON větev, která tiery
        skutečně smazala; kdyby zůstala, `/removeplayertiers` by v produkci
        tiše neudělal nic a admin by si myslel, že ano. Příkaz proto zůstává
        (aby se nezmenšilo API), ale už nikdy nemění stav — a říká přesně, jak
        tier opravdu zrušit.
        """
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        return await interaction.response.send_message(
            "❌ **V PostgreSQL režimu nelze tiery mazat.**\n"
            "Current tier je zrcadlo toho, co potvrzuje Discord, a zrcadlo "
            "nemá clear operaci (viz `MirrorRepository`: chybějící role je "
            "anomálie, ne smazání). Tier zrušíš tak, že **odstraníš Discord "
            "tier roli** a pak spustíš `/sync discord` (Discord → "
            "PostgreSQL). Historie zůstává nedotčená.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Results(bot))
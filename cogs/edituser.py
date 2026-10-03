"""Centrální admin editor hráče – /edituser (interaktivní Discord UI).

Jeden editor, žádné paralelní systémy: všechno se recykluje z existujících
služeb (viz ``services/edituser.py``). Web má jednoho zapisovatele
(``services/websync``), role recyklují RoleSyncService, cooldowny počítají
stávající služby a identity jde přes PlayerIdentityService.

Bezpečnostní model
------------------
- Pouze admini (``has_admin_role`` / ``ADMIN_ROLE_IDS``) – kontrola v KAŽDÉM
  callbacku, nejen u příkazu,
- NIC se neaplikuje bez potvrzení: každá změna skončí v
  :class:`ConfirmEditView` („Potvrdit a aplikovat“ / „Zrušit“),
- mezi náhledem a potvrzením se ověří, že se stará hodnota nezměnila
  (stale check pro identity/IGN/tier; cooldowny jsou stavově přechodné),
- po potvrzení se spustí ``execute_player_edit``: DB (transakce + audit) →
  Discord role (best effort) → web (best effort) s reportem **SUCCESS /
  PARTIAL SUCCESS / FAILURE**.

Tok
---
``/edituser player:@User`` → editor hráče (ephemeral message)
    ├─ 🎭 Změnit Discord ID → modál → potvrzení
    ├─ ⚔️ Změnit IGN        → modál → potvrzení
    ├─ 🏆 Změnit tier kitu  → TierKitSelectView → TierSelectView → potvrzení
    ├─ ⏳ Cooldowny         → CooldownEditView → potvrzení
    ├─ 📜 Historie          → náhled (read-only, editor historie NEMÁ)
    └─ ✖️ Zavřít

Modál nemá přístup k ``interaction.message``, proto se potvrzení z modálu
posílá jako nová ephemeral zpráva; změny uvnitř zprávy editoru (tiery,
cooldowny) se potvrzují přepisem té samé zprávy.
"""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import HT3_COOLDOWN_MS, PLAYER_COOLDOWN_MS
from cogs._shared import apply_role_actions, kit_display_map, member_to_dict
from services import edituser as edituser_service
from services.edituser import (
    STATUS_FAILURE,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    InvalidTierEdit,
    change_kit_tier,
    change_player_discord,
    change_player_ign,
    execute_player_edit,
    format_duration,
    normalize_tier_choice,
    retired_tier_choices,
)
from services.player_identity import (
    CLAIM_UNCHANGED,
    PlayerIdentityConflict,
)
from services.permissions import has_admin_role
from services.websync import sync_website
from services.kit_catalog import get_kits
from utils import DEFAULT_KITS
from views import SafeModal, SafeView

log = logging.getLogger("dachshundtiers")


# ---------------------------------------------------------------------------
# Pomocné funkce (embed + data)
# ---------------------------------------------------------------------------
def _member_to_dict(member) -> dict:
    return member_to_dict(member)


def _current_tier(player: dict, kit_key: str) -> str:
    """Tier kitu v ``modes`` (case-insensitive klíč), nebo ''."""
    kit_low = (kit_key or "").strip().lower()
    for k, v in (player.get("modes") or {}).items():
        if str(k).strip().lower() == kit_low:
            return str(v or "").strip().upper()
    return ""


def _cog_session_factory(cog):
    """session_factory bota – vzor cogs/ht3.py."""
    return getattr(getattr(cog, "bot", None), "db_session_factory", None)


async def _find_player(player_id: str, *, session_factory) -> dict | None:
    """Kanonický tvar hráče z PostgreSQL (projekce přes player_export).

    Bez ``session_factory`` je to chyba, ne „hledat v souboru": editor
    pracuje nad zrcadlem, které potvrdil Discord, a hráč, kterého tu není,
    v databázi opravdu neexistuje.
    """
    if session_factory is None:
        raise RuntimeError(
            "_find_player potřebuje PostgreSQL; players.json se už nepoužívá"
        )
    from db.repositories.players import PlayerRepository
    from db.services.session import transaction as _db_tx
    from services.player_export import build_player_shape

    did = int(player_id) if str(player_id).isdigit() else None
    if did is None:
        return None
    async with _db_tx(session_factory) as session:
        player = await PlayerRepository().get_by_discord_id(session, did)
        if player is None:
            return None
        return await build_player_shape(session, player)


def _editor_embed(player: dict) -> discord.Embed:
    """Hlavní menu editoru – přehled hráče."""
    did = str(player.get("discordId") or "")
    lines = [
        f"**IGN:** `{player.get('username') or '—'}`",
        f"**Discord:** <@{did}> (`{did or '—'}`)",
    ]
    modes = player.get("modes") or {}
    if isinstance(modes, dict) and modes:
        tiers = "\n".join(
            f"  • `{k}` → `{v}`" for k, v in sorted(modes.items(), key=lambda kv: str(kv[0]).lower())
        )
        lines.append(f"**Tiery:**\n{tiers}")
    embed = discord.Embed(
        title="🛠️ /edituser – editor hráče",
        description="\n".join(lines),
        color=0x3B82F6,
    )
    embed.set_footer(
        text="Žádná změna se NEaplikuje bez potvrzení. Audit: PostgreSQL (audit_logs)"
    )
    return embed


def _not_found_embed(player: discord.User) -> discord.Embed:
    return discord.Embed(
        title="❌ Hráč nebyl nalezen",
        description=(
            f"Hráč **{player}** (`{player.id}`) nemá záznam v databázi.\n"
            "`/edituser` edituje EXISTUJÍCÍ záznam – hráč musí mít alespoň "
            "jeden `/result` (nebo být v databázi ručně)."
        ),
        color=0xEF4444,
    )


def _tier_select_embed(player: dict, kit_key: str, old: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏆 Změna tieru – `{kit_key}`",
        description=(
            f"**Hráč:** <@{player.get('discordId') or '0'}> "
            f"(`{player.get('username') or '—'}`)\n"
            f"**Aktuální tier:** `{old or '—'}`\n\n"
            "Vyber novou hodnotu z menu. Retired varianty (R-prefix) znamenají "
            "archivaci – NIKDY nepřepíšou aktuální tier."
        ),
        color=0x3B82F6,
    )
    return embed


async def _cooldown_embed(
    player: dict,
    now: int,
    kit_key: str | None = None,
    *,
    session_factory=None,
) -> discord.Embed:
    pid = str(player.get("discordId") or "")
    from services.cooldowns import get_cooldowns

    snapshot = await get_cooldowns(pid, session_factory=session_factory)
    waitlist_bucket = snapshot.get("waitlist") or {}
    lines = []
    if waitlist_bucket:
        lines.append("**Waitlist (4 d per kit):**")
        for k in sorted(waitlist_bucket):
            marker = "🟡" if k == kit_key else "▫"
            lines.append(f"  {marker} `{k}`: {format_duration(waitlist_bucket[k])}")
    else:
        lines.append("**Waitlist (4 d per kit):** žádné aktivní cooldowny")
    legacy_global_ms = snapshot.get("waitlist_legacy_global_ms")
    if legacy_global_ms is not None:
        lines.append(
            f"  ⚠️ starý globální cooldown (blokuje VŠECHNY kity): "
            f"{format_duration(legacy_global_ms)}"
        )
    ht3_bucket = snapshot.get("ht3") or {}
    if ht3_bucket:
        lines.append("**HT3+ (7 d per kit):**")
        for k in sorted(ht3_bucket):
            marker = "🟡" if k == kit_key else "▫"
            lines.append(f"  {marker} `{k}`: {format_duration(ht3_bucket[k])}")
    else:
        lines.append("**HT3+ (7 d per kit):** žádné aktivní cooldowny")
    if kit_key:
        lines.append(f"\n🟡 Vybrán kit **`{kit_key}`** pro cooldown úpravu.")
    embed = discord.Embed(
        title=f"⏳ Cooldowny – <@{pid}> (`{player.get('username') or '—'}`)",
        description="\n".join(lines),
        color=0x10B981,
    )
    embed.set_footer(text="Úprava cooldownu se aplikuje jen po potvrzení.")
    return embed


def _confirm_embed(payload: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ Potvrzení – {payload['title']}",
        description=(
            f"**Hráč:** <@{payload['player_id']}>\n\n"
            + "\n".join(payload.get("lines") or [])
        ),
        color=0xF59E0B,
    )
    embed.set_footer(
        text="Nic se NEaplikuje, dokud nepotvrdíš tlačítkem. Audit: PostgreSQL (audit_logs)"
    )
    return embed


def _stale_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="🔄 /edituser – stav se změnil",
        description=f"{message}\n\n**Nic se neaplikovalo.** Otevři editor znovu.",
        color=0xEF4444,
    )


def _history_embed(player: dict) -> discord.Embed:
    did = str(player.get("discordId") or "")
    history = player.get("history") or {}
    lines = []
    if isinstance(history, dict) and history:
        for kit_key in sorted(history, key=str.lower):
            entries = [e for e in (history.get(kit_key) or []) if isinstance(e, dict)]
            recent = entries[-8:][::-1]  # nejnovější první
            parts = " · ".join(
                f"{e.get('date') or '?'}: `{e.get('tier') or '?'}`" for e in recent
            )
            extra = f" (+{len(entries) - len(recent)} starších)" if len(entries) > len(recent) else ""
            lines.append(f"• **`{kit_key}`** ({len(entries)} záznamů): {parts}{extra}")
    else:
        lines.append("Žádná historie (editor ji NIKDY nemění ani nemaže).")
    embed = discord.Embed(
        title=f"📜 Historie tierů – <@{did}> (`{player.get('username') or '—'}`)",
        description="\n".join(lines),
        color=0x8B5CF6,
    )
    embed.set_footer(text="Historie je read-only – zapisuje ji jen /result a /topresult.")
    return embed


def _report_embed(report: dict, payload: dict) -> discord.Embed:
    """Závěrečný report SUCCESS / PARTIAL SUCCESS / FAILURE."""
    status = report.get("status", STATUS_FAILURE)
    colors = {
        STATUS_SUCCESS: 0x10B981,
        STATUS_PARTIAL: 0xF59E0B,
        STATUS_FAILURE: 0xEF4444,
    }
    icons = {
        STATUS_SUCCESS: "✅",
        STATUS_PARTIAL: "⚠️",
        STATUS_FAILURE: "❌",
    }
    embed = discord.Embed(
        title=f"{icons.get(status, '❌')} /edituser – {status}",
        description=f"**{payload['title']}** – hráč <@{payload['player_id']}>",
        color=colors.get(status, 0xEF4444),
    )
    lines = payload.get("lines") or []
    if lines:
        embed.add_field(name="Změna", value="\n".join(lines), inline=False)

    db = report.get("db") or {}
    if db.get("status") in ("error", "not_found"):
        embed.add_field(
            name="Chyba",
            value=str(db.get("message") or report.get("message") or "?"),
            inline=False,
        )
    else:
        if db.get("status") == "unchanged":
            embed.add_field(
                name="Výsledek",
                value="Beze změny (idempotentní opakování stejné úpravy).",
                inline=False,
            )
        else:
            msg = report.get("message") or ""
            if msg:
                embed.add_field(name="Synchronizace", value=msg, inline=False)
            roles = report.get("roles") or {}
            if roles.get("actions"):
                actions = roles["actions"]
                ok_n = sum(1 for a in actions if a.get("ok"))
                val = f"**{ok_n}/{len(actions)}** akcí OK"
                errs = [e for e in (roles.get("errors") or []) if e]
                if errs:
                    val += "\n❌ " + "\n❌ ".join(str(e) for e in errs)
                embed.add_field(name="Discord role", value=val, inline=False)
            elif roles.get("skipped") and roles.get("note"):
                embed.add_field(
                    name="Discord role",
                    value="⏭️ " + str(roles["note"]),
                    inline=False,
                )
            web = report.get("web") or {}
            if not web.get("skipped"):
                wval = "✅ OK" if web.get("ok") else "❌ SELHALO"
                errs = [e for e in (web.get("errors") or []) if e]
                if errs:
                    wval += "\n" + "\n".join(f"  • {e}" for e in errs)
                embed.add_field(name="Web (GitHub)", value=wval, inline=False)
    warnings = report.get("warnings") or []
    if warnings:
        embed.add_field(name="Pozor", value="\n".join(warnings), inline=False)
    embed.set_footer(text="Audit: PostgreSQL (audit_logs)")
    return embed


# ---------------------------------------------------------------------------
# Modály (Discord ID / IGN) – potvrzení jde do nové ephemeral zprávy
# ---------------------------------------------------------------------------
class DiscordIdModal(SafeModal):
    def __init__(self, *, cog, player_id: str):
        super().__init__(title="🆔 Změna Discord ID hráče")
        self.cog = cog
        self.player_id = player_id
        self.input = discord.ui.TextInput(
            label="Nové Discord ID (17–19 číslic)",
            placeholder="např. 123456789012345678",
            min_length=15,
            max_length=19,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        new_id = (self.input.value or "").strip()
        p = await _find_player(
            self.player_id, session_factory=_cog_session_factory(self.cog)
        )
        if p is None:
            return await interaction.followup.send(
                embed=_stale_embed("Hráč už v databázi není."), ephemeral=True
            )
        old = str(p.get("discordId") or "")
        try:
            _new_players, _target, outcome = change_player_discord(
                [p], {"discordId": self.player_id}, new_id
            )
        except PlayerIdentityConflict as exc:
            return await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        if outcome == CLAIM_UNCHANGED:
            return await interaction.followup.send(
                "ℹ️ Beze změny – dané Discord ID má hráč už nastavené.",
                ephemeral=True,
            )
        payload = _confirm_payload(
            player_id=self.player_id,
            edit={"field": "discord_id", "new_value": new_id},
            title="Změna Discord ID",
            lines=[
                f"`{old or '—'}` → **<@{new_id}>**",
                "Discord ID je **primární identita**: nevznikne nový hráč, "
                "history/tiery/výsledky/tickety/cooldowny zůstávají.",
            ],
            stale={"field": "discord_id", "old_value": old},
        )
        embed = _confirm_embed(payload)
        view = ConfirmEditView(cog=self.cog, payload=payload)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class IgnModal(SafeModal):
    def __init__(self, *, cog, player_id: str):
        super().__init__(title="⚔️ Změna IGN hráče")
        self.cog = cog
        self.player_id = player_id
        self.input = discord.ui.TextInput(
            label="Nové Minecraft IGN",
            placeholder="např. Adrison99",
            min_length=2,
            max_length=16,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        new_ign = (self.input.value or "").strip()
        p = await _find_player(
            self.player_id, session_factory=_cog_session_factory(self.cog)
        )
        if p is None:
            return await interaction.followup.send(
                embed=_stale_embed("Hráč už v databázi není."), ephemeral=True
            )
        old = str(p.get("username") or "")
        try:
            _new_players, target, outcome = change_player_ign(
                [p], {"discordId": self.player_id}, new_ign
            )
        except PlayerIdentityConflict as exc:
            return await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        if outcome == CLAIM_UNCHANGED and str(target.get("username") or "") == old:
            return await interaction.followup.send(
                "ℹ️ Beze změny – hráč toto IGN už má.", ephemeral=True
            )
        payload = _confirm_payload(
            player_id=self.player_id,
            edit={"field": "ign", "new_value": new_ign},
            title="Změna IGN",
            lines=[f"`{old or '—'}` → **`{new_ign}`**"],
            stale={"field": "ign", "old_value": old},
        )
        embed = _confirm_embed(payload)
        view = ConfirmEditView(cog=self.cog, payload=payload)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


def _confirm_payload(*, player_id, edit, title, lines, stale=None, back_to_main=False) -> dict:
    return {
        "player_id": player_id,
        "edit": edit,
        "title": title,
        "lines": lines,
        "stale": stale,
        "back_to_main": back_to_main,
    }


# ---------------------------------------------------------------------------
# Hlavní editor (menu) + podviewy
# ---------------------------------------------------------------------------
class PlayerEditorView(SafeView):
    """Hlavní menu: přehled hráče + akce."""

    def __init__(self, *, cog, player_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id

    async def _admin(self, interaction) -> bool:
        if not has_admin_role(interaction.user):
            await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
            return False
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
            return False
        return True

    async def _swap(self, interaction, embed, view) -> None:
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser: %s", err)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🎭 Změnit Discord ID", style=discord.ButtonStyle.secondary, custom_id="edituser_discord")
    async def on_discord(self, interaction, button) -> None:
        if not await self._admin(interaction):
            return
        sf = _cog_session_factory(self.cog)
        if await _find_player(self.player_id, session_factory=sf) is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        await interaction.response.send_modal(
            DiscordIdModal(cog=self.cog, player_id=self.player_id)
        )

    @discord.ui.button(label="⚔️ Změnit IGN", style=discord.ButtonStyle.secondary, custom_id="edituser_ign")
    async def on_ign(self, interaction, button) -> None:
        if not await self._admin(interaction):
            return
        sf = _cog_session_factory(self.cog)
        if await _find_player(self.player_id, session_factory=sf) is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        await interaction.response.send_modal(
            IgnModal(cog=self.cog, player_id=self.player_id)
        )

    @discord.ui.button(label="🏆 Změnit tier kitu", style=discord.ButtonStyle.primary, custom_id="edituser_tiers")
    async def on_tiers(self, interaction, button) -> None:
        if not await self._admin(interaction):
            return
        view = TierKitSelectView(
            cog=self.cog,
            player_id=self.player_id,
            kits=await get_kits(session_factory=_cog_session_factory(self.cog)),
        )
        embed = await view._embed()
        await self._swap(interaction, embed, view)

    @discord.ui.button(label="⏳ Cooldowny", style=discord.ButtonStyle.primary, custom_id="edituser_cooldown")
    async def on_cooldown(self, interaction, button) -> None:
        if not await self._admin(interaction):
            return
        now = int(time.time() * 1000)
        sf = _cog_session_factory(self.cog)
        view = CooldownEditView(
            cog=self.cog,
            player_id=self.player_id,
            kits=await get_kits(session_factory=_cog_session_factory(self.cog)),
        )
        player = await _find_player(self.player_id, session_factory=sf)
        embed = await _cooldown_embed(player or {}, now, session_factory=sf)
        await self._swap(interaction, embed, view)

    @discord.ui.button(label="📜 Historie", style=discord.ButtonStyle.secondary, custom_id="edituser_history")
    async def on_history(self, interaction, button) -> None:
        if not await self._admin(interaction):
            return
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf)
        if player is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        view = HistoryView(cog=self.cog, player_id=self.player_id)
        await self._swap(interaction, _history_embed(player), view)

    @discord.ui.button(label="✖️ Zavřít", style=discord.ButtonStyle.danger, custom_id="edituser_close")
    async def on_close(self, interaction, button) -> None:
        if not await self._admin(interaction):
            return
        self.stop()
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.edit_original_response(view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze odebrat view /edituser: %s", err)
        await interaction.followup.send(
            "✖ Editor zavřen – nic se nezměnilo.", ephemeral=True
        )


class TierKitSelectView(SafeView):
    """1) Výběr kitu pro změnu tieru."""

    def __init__(self, *, cog, player_id: str, kits: list[str] | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id
        # Katalog předává volající (z DB přes services.kit_catalog) – tady se
        # žádný soubor nečte, protože __init__ nemá session factory.
        kits = list(kits) if kits else list(DEFAULT_KITS)
        select = discord.ui.Select(
            custom_id="edituser_tier_kit",
            placeholder="Vyber kit, jehož tier chceš změnit...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=str(k), value=str(k)) for k in kits[:25]
            ],
        )
        select.callback = self.on_kit
        self.add_item(select)

    async def _embed(self) -> discord.Embed:
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf) or {}
        embed = discord.Embed(
            title="🏆 Změna tieru kitu",
            description=(
                f"**Hráč:** <@{self.player_id}> (`{player.get('username') or '—'}`)\n\n"
                "Vyber kit. Pak zvolíš novou hodnotu tieru (aktuální nebo retired)."
            ),
            color=0x3B82F6,
        )
        return embed

    async def on_kit(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await interaction.response.send_message(
                "ℹ️ Vyber kit z menu.", ephemeral=True
            )
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf)
        if player is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        kit_key = values[0]
        old = _current_tier(player, kit_key)
        view = TierSelectView(cog=self.cog, player_id=self.player_id, kit_key=kit_key)
        embed = _tier_select_embed(player, kit_key, old)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (kit): %s", err)

    @discord.ui.button(label="↩️ Zpět", style=discord.ButtonStyle.secondary, custom_id="edituser_tierkit_back")
    async def on_back(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf)
        if player is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        view = PlayerEditorView(cog=self.cog, player_id=self.player_id)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(
                    embed=_editor_embed(player), view=view
                )
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (zpět): %s", err)


class TierSelectView(SafeView):
    """2) Výběr nové hodnoty tieru (aktuální / retired, žádný free-text)."""

    def __init__(self, *, cog, player_id: str, kit_key: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id
        self.kit_key = kit_key
        current = [
            discord.SelectOption(label=t, value=t, description="aktuální tier")
            for t in edituser_service.current_tier_choices()
        ]
        retired = [
            discord.SelectOption(
                label=t, value=t, description="retired (archivace historie)"
            )
            for t in retired_tier_choices()
        ]
        for custom_id, placeholder, options in (
            ("edituser_tier_value", "Aktuální tier...", current),
            ("edituser_tier_value_retired", "Retired tier (archivace)...", retired),
        ):
            select = discord.ui.Select(
                custom_id=custom_id,
                placeholder=placeholder,
                min_values=1,
                max_values=1,
                options=options[:25],
            )
            select.callback = self.on_tier
            self.add_item(select)

    async def on_tier(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await interaction.response.send_message(
                "ℹ️ Vyber tier z menu.", ephemeral=True
            )
        value = values[0]
        retired = (value or "").startswith("R")
        shape = await _find_player(
            self.player_id, session_factory=_cog_session_factory(self.cog)
        ) or {}
        try:
            _new_players, target, old, outcome = change_kit_tier(
                [shape],
                {"discordId": self.player_id},
                self.kit_key,
                value,
                retired=retired,
            )
        except InvalidTierEdit as exc:
            return await interaction.response.send_message(
                f"❌ {exc}", ephemeral=True
            )
        stored = normalize_tier_choice(value, retired=retired)
        kit_display = self.kit_key
        if outcome == edituser_service.OUTCOME_UNCHANGED:
            return await interaction.response.send_message(
                f"ℹ️ Beze změny – kit `{kit_display}` už má tier `{old or '—'}`.",
                ephemeral=True,
            )
        payload = _confirm_payload(
            player_id=self.player_id,
            edit={
                "field": "tier",
                "kit": self.kit_key,
                "tier": value,
                "retired": retired,
            },
            title=f"Změna tieru – {kit_display}",
            lines=[
                f"`{kit_display}`: `{old or '—'}` → **`{stored}`**"
            ],
            stale={"field": "tier", "old_value": old or "", "kit": self.kit_key},
            back_to_main=True,
        )
        embed = _confirm_embed(payload)
        view = ConfirmEditView(cog=self.cog, payload=payload)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (tier): %s", err)

    @discord.ui.button(label="↩️ Zpět", style=discord.ButtonStyle.secondary, custom_id="edituser_tier_back")
    async def on_back(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf)
        if player is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        view = PlayerEditorView(cog=self.cog, player_id=self.player_id)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(
                    embed=_editor_embed(player), view=view
                )
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (zpět tier): %s", err)


class CooldownEditView(SafeView):
    """Přehled + úprava cooldownů (stávající výpočty/soubory)."""

    def __init__(
        self,
        *,
        cog,
        player_id: str,
        kit_key: str | None = None,
        kits: list[str] | None = None,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id
        self.kit_key = kit_key
        # Viz TierKitSelectView: katalog předává volající, tady se nic nečte.
        kits = list(kits) if kits else list(DEFAULT_KITS)
        select = discord.ui.Select(
            custom_id="edituser_cd_kit",
            placeholder="Vyber kit pro HT3+ cooldown...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=str(k), value=str(k)) for k in kits[:25]
            ],
        )
        select.callback = self.on_kit
        self.add_item(select)

    async def _embed(self) -> discord.Embed:
        now = int(time.time() * 1000)
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf) or {}
        return await _cooldown_embed(player, now, self.kit_key, session_factory=sf)

    async def on_kit(self, interaction: discord.Interaction) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        values = (interaction.data or {}).get("values") or []
        if not values:
            return await interaction.response.send_message(
                "ℹ️ Vyber kit z menu.", ephemeral=True
            )
        view = CooldownEditView(
            cog=self.cog,
            player_id=self.player_id,
            kit_key=values[0],
            kits=await get_kits(session_factory=_cog_session_factory(self.cog)),
        )
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=await view._embed(), view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (cooldown kit): %s", err)

    async def _preview_lines(self, action: str, kit_key: str | None) -> list:
        from services.cooldowns import get_cooldowns

        snapshot = await get_cooldowns(
            self.player_id, session_factory=_cog_session_factory(self.cog)
        )
        if action in ("clear_queue", "set_queue"):
            q_old = format_duration((snapshot.get("waitlist") or {}).get(kit_key))
            if action == "clear_queue":
                return [f"waitlist {kit_key}: `{q_old}` → **žádný**"]
            return [
                f"waitlist {kit_key}: `{q_old}` → "
                f"**{format_duration(PLAYER_COOLDOWN_MS)} (od teď)**"
            ]
        kit_key = (kit_key or "").strip().lower()
        h_old = format_duration((snapshot.get("ht3") or {}).get(kit_key))
        if action == "clear_ht3":
            return [f"HT3 `{kit_key}`: `{h_old}` → **žádný**"]
        if action == "set_ht3":
            return [
                f"HT3 `{kit_key}`: `{h_old}` → "
                f"**{format_duration(HT3_COOLDOWN_MS)} (od teď)**"
            ]
        return [str(action)]

    async def _to_confirm(self, interaction, action: str, kit_key: str | None, title: str) -> None:
        edit = {"field": "cooldown", "action": action}
        if kit_key:
            edit["kit"] = kit_key
        payload = _confirm_payload(
            player_id=self.player_id,
            edit=edit,
            title=title,
            lines=await self._preview_lines(action, kit_key),
            stale=None,
            back_to_main=True,
        )
        embed = _confirm_embed(payload)
        view = ConfirmEditView(cog=self.cog, payload=payload)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=embed, view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (cooldown): %s", err)

    @discord.ui.button(label="🧹 Smazat waitlist kitu", style=discord.ButtonStyle.secondary, custom_id="edituser_cd_clear_q")
    async def on_clear_queue(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if not self.kit_key:
            return await interaction.response.send_message(
                "ℹ️ Nejdřív vyber kit v menu nahoře.", ephemeral=True
            )
        await self._to_confirm(
            interaction, "clear_queue", self.kit_key, f"Smazat waitlist cooldown – {self.kit_key}"
        )

    @discord.ui.button(label="⏱️ Waitlist na 4 d", style=discord.ButtonStyle.secondary, custom_id="edituser_cd_set_q")
    async def on_set_queue(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if not self.kit_key:
            return await interaction.response.send_message(
                "ℹ️ Nejdřív vyber kit v menu nahoře.", ephemeral=True
            )
        await self._to_confirm(
            interaction, "set_queue", self.kit_key, f"Nastavit waitlist cooldown 4 d – {self.kit_key}"
        )

    @discord.ui.button(label="🧹 Smazat HT3 cooldown kitu", style=discord.ButtonStyle.secondary, custom_id="edituser_cd_clear_ht3")
    async def on_clear_ht3(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if not self.kit_key:
            return await interaction.response.send_message(
                "ℹ️ Nejdřív vyber kit v menu nahoře.", ephemeral=True
            )
        await self._to_confirm(
            interaction, "clear_ht3", self.kit_key, f"Smazat HT3 cooldown – {self.kit_key}"
        )

    @discord.ui.button(label="⏱️ HT3 na 7 d", style=discord.ButtonStyle.secondary, custom_id="edituser_cd_set_ht3")
    async def on_set_ht3(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if not self.kit_key:
            return await interaction.response.send_message(
                "ℹ️ Nejdřív vyber kit v menu nahoře.", ephemeral=True
            )
        await self._to_confirm(
            interaction, "set_ht3", self.kit_key, f"Nastavit HT3 cooldown 7 d – {self.kit_key}"
        )

    @discord.ui.button(label="↩️ Zpět", style=discord.ButtonStyle.secondary, custom_id="edituser_cd_back")
    async def on_back(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf)
        if player is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        view = PlayerEditorView(cog=self.cog, player_id=self.player_id)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=_editor_embed(player), view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (zpět cd): %s", err)


class HistoryView(SafeView):
    """Náhled historie tierů (read-only – editor jí NIKDY nemění)."""

    def __init__(self, *, cog, player_id: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.player_id = player_id

    @discord.ui.button(label="↩️ Zpět", style=discord.ButtonStyle.secondary, custom_id="edituser_history_back")
    async def on_back(self, interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.player_id, session_factory=sf)
        if player is None:
            return await interaction.response.send_message(
                "❌ Hráč už v databázi není.", ephemeral=True
            )
        view = PlayerEditorView(cog=self.cog, player_id=self.player_id)
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=_editor_embed(player), view=view)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze překreslit view /edituser (zpět hist): %s", err)


class ConfirmEditView(SafeView):
    """Finální potvrzení: nic se NEaplikuje před tímto tlačítkem.

    Po potvrzení běží ``execute_player_edit`` (DB → Discord role → web) a
    zpráva se přepíše reportem SUCCESS / PARTIAL SUCCESS / FAILURE.
    """

    def __init__(self, *, cog, payload: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.payload = payload
        self.finished = False

    async def _stale_check(self) -> str | None:
        stale = self.payload.get("stale")
        if not stale:
            return None
        sf = _cog_session_factory(self.cog)
        player = await _find_player(self.payload["player_id"], session_factory=sf)
        if player is None:
            return "Hráč už v databázi není."
        field = stale.get("field")
        old = str(stale.get("old_value") or "")
        if field == "discord_id":
            cur = str(player.get("discordId") or "")
            if cur != old:
                return (
                    f"Discord ID hráče se mezitím změnilo "
                    f"(`{cur}` místo `{old}`) – opakuj úpravu."
                )
        if field == "ign":
            cur = str(player.get("username") or "")
            if cur.lower() != old.lower():
                return (
                    f"IGN hráče se mezitím změnilo "
                    f"(`{cur}` místo `{old}`) – opakuj úpravu."
                )
        if field == "tier":
            cur = _current_tier(player, str(stale.get("kit") or ""))
            if cur != old.upper():
                return (
                    f"Tier kitu `{stale.get('kit')}` se mezitím změnil "
                    f"(`{cur or '—'}` místo `{old or '—'}`) – opakuj úpravu."
                )
        return None

    @discord.ui.button(label="✅ Potvrdit a aplikovat", style=discord.ButtonStyle.success, custom_id="edituser_confirm")
    async def on_confirm(self, interaction: discord.Interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if self.finished:
            return await interaction.response.send_message(
                "✅ Změny už byly aplikované.", ephemeral=True
            )
        self.finished = True

        await interaction.response.defer(ephemeral=True)

        # 1) Stale check – aplikujeme PŘESNĚ to, co admin viděl.
        stale_msg = await self._stale_check()
        if stale_msg:
            embed = _stale_embed(stale_msg)
            await self._finish_message(interaction, embed)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # 2) Aplikace: DB (transakce+audit) → Discord role → web (best effort).
        edit = self.payload["edit"]
        role_context = None
        apply_roles = None
        if edit.get("field") == "tier":
            role_context = await self.cog._role_context(
                interaction.guild, self.payload["player_id"]
            )

            async def apply_roles(actions):
                return await self.cog._apply_roles(interaction.guild, actions)

        push_web = None
        if edit.get("field") in ("tier", "ign", "discord_id"):
            actor_id = interaction.user.id
            actor_name = str(interaction.user)

            async def push_web(canonical):
                return await sync_website(
                    canonical=canonical,
                    message="edituser: aktualizace hráče na web",
                    actor_id=actor_id,
                    actor_name=actor_name,
                )

        report = await execute_player_edit(
            player_id=self.payload["player_id"],
            edit=edit,
            actor_id=interaction.user.id,
            actor_name=str(interaction.user),
            queue_cooldown_ms=PLAYER_COOLDOWN_MS,
            ht3_cooldown_ms=HT3_COOLDOWN_MS,
            role_context=role_context,
            apply_roles=apply_roles,
            push_web=push_web,
            session_factory=_cog_session_factory(self.cog),
        )

        embed = _report_embed(report, self.payload)
        await self._finish_message(interaction, embed)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="✖️ Zrušit", style=discord.ButtonStyle.danger, custom_id="edituser_cancel")
    async def on_cancel(self, interaction: discord.Interaction, button) -> None:
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )
        self.finished = True
        if self.payload.get("back_to_main"):
            sf = _cog_session_factory(self.cog)
            player = await _find_player(self.payload["player_id"], session_factory=sf)
            if player is not None:
                view = PlayerEditorView(cog=self.cog, player_id=self.payload["player_id"])
                await interaction.response.defer(ephemeral=True)
                self.stop()
                try:
                    if interaction.message is not None:
                        await interaction.edit_original_response(
                            embed=_editor_embed(player), view=view
                        )
                except (discord.HTTPException, discord.Forbidden) as err:
                    log.warning("Nelze vrátit view /edituser: %s", err)
                return
        embed = discord.Embed(
            title="✖ Zrušeno",
            description="Nic se nezměnilo – žádná úprava se neaplikovala.",
            color=0x9CA3AF,
        )
        await interaction.response.defer(ephemeral=True)
        self.stop()
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit zrušenou zprávu /edituser: %s", err)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _finish_message(self, interaction, embed) -> None:
        try:
            if interaction.message is not None:
                await interaction.edit_original_response(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden) as err:
            log.warning("Nelze upravit report /edituser: %s", err)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class EditUser(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /edituser
    # ------------------------------------------------------------------
    @app_commands.command(
        name="edituser",
        description="Otevře admin editor hráče (Discord ID, IGN, tiery, cooldowny)",
    )
    @app_commands.describe(
        player="Hráč, jehož data chceš upravit (musí mít záznam v PostgreSQL)"
    )
    async def edituser(
        self, interaction: discord.Interaction, player: discord.User
    ) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        player_id = str(player.id)
        record = await _find_player(
            player_id,
            session_factory=getattr(getattr(self, "bot", None), "db_session_factory", None),
        )
        if record is None:
            return await interaction.followup.send(
                embed=_not_found_embed(player), ephemeral=True
            )

        view = PlayerEditorView(cog=self, player_id=player_id)
        await interaction.followup.send(
            embed=_editor_embed(record), view=view, ephemeral=True
        )

    # ------------------------------------------------------------------
    # /linkdiscord – admin propojí IGN ↔ Discord ID za hráče (stejná pravidla
    # jako /linkign, viz services/player_link).
    # ------------------------------------------------------------------
    @app_commands.command(
        name="linkdiscord",
        description="Propojí Minecraft IGN s Discord ID hráče (explicitní claim)",
    )
    @app_commands.describe(
        player="Hráč (Discord uživatel), kterému se IGN přiřazuje",
        ign="Minecraft IGN, který se k hráči připojí",
    )
    async def linkdiscord(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        ign: str,
    ) -> None:
        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Pouze na serveru.", ephemeral=True
            )
        if not has_admin_role(interaction.user):
            return await interaction.response.send_message(
                "❌ Pouze pro administrátory.", ephemeral=True
            )

        ign_clean = (ign or "").strip()
        if not ign_clean:
            return await interaction.response.send_message(
                "❌ IGN nesmí být prázdné.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        session_factory = getattr(self.bot, "db_session_factory", None)
        if session_factory is None:
            return await interaction.followup.send(
                "❌ PostgreSQL není nakonfigurováno – /linkdiscord je po Phase D "
                "cutoveru PostgreSQL-first a bez DB se NEPROVÁDÍ (žádný JSON-only "
                "claim, no silent fallback).",
                ephemeral=True,
            )

        from cogs.link import mirror_member
        from services.player_link import (
            LINK_ADOPTED,
            LINK_CREATED,
            LINK_MERGED,
            LINK_RENAMED,
            LinkRefused,
            link_ign,
        )

        try:
            outcome = await link_ign(
                player.id,
                ign_clean,
                session_factory=session_factory,
                actor_id=interaction.user.id,
                actor_name=str(interaction.user),
            )
        except LinkRefused as err:
            return await interaction.followup.send(
                f"**{ign_clean}** – {err}", ephemeral=True
            )
        member = interaction.guild.get_member(player.id)
        if member is not None:
            await mirror_member(session_factory, member, command="/linkdiscord")

        outcome_label = {
            LINK_CREATED: "🎉 Vytvořen nový hráč",
            LINK_RENAMED: "✏️ IGN změněno",
            LINK_ADOPTED: "🔗 Propojeno k existujícímu záznamu",
            LINK_MERGED: "🔗 Propojeno, dřívější záznam sloučen",
        }.get(outcome.status, "✅ Už propojeno – nic se nezměnilo")
        embed = discord.Embed(
            title="🔗 Propojení identity",
            description=(
                f"**`{ign_clean}`** ↔ <@{player.id}> ({outcome_label})"
            ),
            color=0x10B981,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # Discord role (RoleSyncService, best effort) – používá ConfirmEditView
    # ------------------------------------------------------------------
    async def _apply_roles(self, guild: discord.Guild, actions: list) -> list:
        """Aplikuje akce (add/remove rolí); každá akce se vyhodnotí zvlášť.

        Sdílená implementace v ``cogs/_shared.apply_role_actions`` (stejný kód
        jako /sync discord) – žádná duplicita role-plan aplikace.
        """
        return await apply_role_actions(guild, actions)

    async def _role_context(self, guild: discord.Guild, player_id: str) -> dict:
        """Kontext pro plán role syncu (normalizovaný member + mapování rolí)."""
        member = None
        if str(player_id).isdigit():
            member = guild.get_member(int(player_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(player_id))
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    member = None
        member_dict = _member_to_dict(member) if member is not None else None
        session_factory = getattr(self.bot, "db_session_factory", None)
        from services.kit_roles import get_all_kit_role_maps

        roles_map = await get_all_kit_role_maps(session_factory=session_factory)
        return {
            "member": member_dict,
            "roles_map": roles_map,
            "kit_display": await kit_display_map(session_factory=session_factory),
        }


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EditUser(bot))
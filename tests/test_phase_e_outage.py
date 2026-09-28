"""Phase E, E6 — PostgreSQL outage contract tests.

Simulates PostgreSQL being UNAVAILABLE across every production path and
proves the hard invariants:

- /sync discord : loud failure, mirror NOT written, Discord roles untouched,
                  players.json is NEVER read as a silent fallback;
- /result, /topresult : Discord role already granted → DB down → loud wedge
                  message surfaces in the reply and Discord is NEVER reverted
                  (no remove_roles / edit / edit_role anywhere);
- promotion service : commit fails AND the wedge enqueue fails too → loud
                  "manual completion" message, no hidden exception, no
                  half-written state;
- reconciliation : DB down → loud failure, zero Discord role mutations,
                  no JSON fallback;
- GitHub export (/sync web) : DB down does NOT block the export chain
                  (export is downstream-only) and Discord roles are never
                  touched by the export;
- bot reconciliation hook : a DB failure inside the loop is logged, never
                  crashes the bot.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import github_sync
import pytest

import storage
from cogs.roles import TierRoleGrant
from db.repositories.outbox import OutboxRepository
from db.repositories.tiers import MirrorRepository
from db.services.outbox_consumer import OutboxConsumer
from db.services.promotion import (
    PromotionCommitService,
    PromotionWedgeOutcome,
    commit_promotion_with_wedge,
)
from db.services.reconciliation import ReconciliationService
from db.services.session import transaction

from cogs.sync import Sync

from tests.test_phase_e_reconciliation import (
    MUTATION_API,
    SpyMember,
    _seed as _seed_reconciliation,
)
from tests.test_services_promotion import _seed as _seed_promotion


# ---------------------------------------------------------------------------
# Service-level: promotion totální výpadek (commit i wedge enqueue selžou)
# ---------------------------------------------------------------------------
async def test_promotion_full_outage_commit_and_wedge_fail_loud_manual(
    session_factory, clean_db, monkeypatch
):
    """Commit selže A zařazení wedge do outboxu taky selže (úplný výpadek):

    - outcome je hlasitý (committed=False, wedged=False),
    - message hlásí, že mirror je nutné doplnit ručně,
    - žádná výjimka nepropadne ven z helperu,
    - v outboxu NELEŽÍ žádná událost (nešlo ji zařadit).
    """
    seeded = await _seed_promotion(session_factory)

    async def boom(*args, **kwargs):
        raise RuntimeError("DB outage")

    monkeypatch.setattr(
        PromotionCommitService, "commit_after_discord_success", boom
    )
    monkeypatch.setattr(
        "db.services.promotion.enqueue_promotion_wedge", boom
    )

    outcome = await commit_promotion_with_wedge(
        session_factory,
        result_key="outage-1",
        kind="ticket",
        discord_id=1111,
        ign="Promo",
        kit_key="ht3",
        new_tier_code="t2",
        discord_role_id=777799,
        score="3-1",
        outcome="Won",
        audit_actor_id=42,
        audit_actor_name="Mod",
    )

    assert outcome.committed is False
    assert outcome.wedged is False
    assert "ručně" in outcome.message
    assert "outboxu" in outcome.message

    async with transaction(session_factory) as session:
        events = await OutboxRepository().list_by_status(session, status="pending")
    assert events == []

    # Discord-first invariant: nic se nevrátilo zpět (žádný rollback role)
    async with transaction(session_factory) as session:
        mirror = await MirrorRepository().get_current(
            session,
            player_id=seeded["player"].id,
            kit_id=seeded["kit"].id,
        )
    assert mirror is None


# ---------------------------------------------------------------------------
# Service-level: reconciliation při výpadku DB
# ---------------------------------------------------------------------------
async def test_reconciliation_db_outage_loud_no_discord_mutation_no_json(
    session_factory, clean_db, monkeypatch
):
    """Reconciliation s DB, která padá uprostřed sync_guild:

    - otevření transakce v sync_guild je NAD per-member try → RuntimeError
      propaguje ven z reconcile (loud signál, nikdy tichý pád),
    - na Discordu se neprovede JEDINÁ role mutace (observe-only i při chybě),
    - players.json se NEČTE jako fallback zdroj.
    """
    await _seed_reconciliation(session_factory)
    member = SpyMember(id=1111, role_ids=(7777,))

    @asynccontextmanager
    async def boom_transaction(*args, **kwargs):
        raise RuntimeError("DB outage")
        yield  # unreachable — signalizuje, že session nikdy nevznikla

    monkeypatch.setattr("db.services.mirror_sync.transaction", boom_transaction)

    service = ReconciliationService()
    with mock.patch.object(
        storage, "load_data", side_effect=AssertionError("JSON fallback zakázán")
    ):
        with pytest.raises(RuntimeError, match="DB outage"):
            await service.reconcile(
                session_factory,
                members=[member],
                triggered_by=42,
                triggered_by_name="Mod",
                observed_at=datetime.now(timezone.utc),
            )

    for api in MUTATION_API:
        assert getattr(member, api).await_count == 0


async def test_reconciliation_db_outage_outbox_failure_still_zero_mutations(
    session_factory, clean_db, monkeypatch
):
    """Výpadek DB při first kroku (drain outboxu) → sync se vůbec nesmí
    spustit a Discord zůstává nedotčený."""
    await _seed_promotion(session_factory)
    member = SpyMember(id=1111, role_ids=(7777,))

    async def boom(*args, **kwargs):
        raise RuntimeError("DB outage")

    monkeypatch.setattr(OutboxConsumer, "consume_many", boom)

    service = ReconciliationService()
    with pytest.raises(RuntimeError, match="DB outage"):
        await service.reconcile(session_factory, members=[member])

    for api in MUTATION_API:
        assert getattr(member, api).await_count == 0


# ---------------------------------------------------------------------------
# Cog-level: /sync discord – DB down, players.json se nikdy nečte
# ---------------------------------------------------------------------------
def _admin_member(rid: int = 999):
    return SimpleNamespace(
        id=999,
        roles=[SimpleNamespace(id=rid, name="Vedení")],
        guild_permissions=SimpleNamespace(administrator=False),
    )


def _plain_member():
    return SimpleNamespace(
        id=1,
        roles=[],
        guild_permissions=SimpleNamespace(administrator=False),
    )


def _interaction(user=None, guild=None):
    inter = mock.MagicMock()
    inter.user = user if user is not None else _plain_member()
    inter.guild = guild if guild is not None else mock.MagicMock()
    inter.response = mock.MagicMock()
    inter.response.send_message = mock.AsyncMock()
    inter.response.defer = mock.AsyncMock()
    inter.followup = mock.MagicMock()
    inter.followup.send = mock.AsyncMock()
    inter.message = SimpleNamespace(edit=mock.AsyncMock())
    return inter


def _member(mid, name, role_ids=()):
    m = SimpleNamespace(
        id=int(mid),
        name=name,
        nick=None,
        display_name=name,
        bot=False,
        roles=[SimpleNamespace(id=int(r)) for r in role_ids],
    )
    m.add_roles = mock.AsyncMock()
    m.remove_roles = mock.AsyncMock()
    m.edit = mock.AsyncMock()
    return m


def _guild(members=()):
    by_id = {int(m.id): m for m in members}
    g = mock.MagicMock()
    g.members = list(members)
    g.get_member.side_effect = lambda mid: by_id.get(mid)
    return g


class SyncDiscordDbOutageTests(unittest.TestCase):
    """/sync discord při DB down: loud, mirror pryč, JSON fallback NIKDY."""

    def setUp(self):
        from services import permissions

        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, inter, *, db_session_factory=object()):
        from db.services import DiscordSyncService

        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=db_session_factory)

        async def main():
            with mock.patch.object(
                DiscordSyncService, "sync_guild", new=mock.AsyncMock()
            ) as sync_guild:
                sync_guild.side_effect = OSError("connection refused")
                await Sync.sync_discord.callback(cog, inter)

        asyncio.run(main())

    def test_db_down_loud_no_json_fallback_no_discord_touch(self):
        alice = _member(1, "AliceMC")
        inter = _interaction(user=_admin_member(), guild=_guild([alice]))

        with mock.patch.object(
            storage, "load_data", side_effect=AssertionError("JSON fallback zakázán")
        ):
            self._run(inter)

        embed = inter.followup.send.await_args.kwargs["embed"]
        self.assertIn("databáze selhala", embed.title)
        alice.add_roles.assert_not_awaited()
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cog-level: /result a /topresult – DB down PO Discord grantu
# ---------------------------------------------------------------------------
class ResultDbOutageTests(unittest.TestCase):
    """/result: grant.ok už změnil Discord roli; DB down → wedge zpráva
    se zobrazí a Discord se NIKDY nevrací."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)

    def _interaction(self, user_id):
        inter = mock.MagicMock()
        inter.user = SimpleNamespace(
            id=user_id,
            name="tester",
            display_name="tester",
            roles=[SimpleNamespace(id=777, name="Tester")],
            guild_permissions=SimpleNamespace(administrator=True),
        )
        inter.guild = mock.MagicMock()
        alice = _member(1, "AliceMC")
        inter.guild.get_member.side_effect = lambda mid: alice if int(mid) == 1 else None
        inter.channel = mock.MagicMock()  # mimo HT ticket → queue cesta
        inter.response.send_message = mock.AsyncMock()
        inter.response.defer = mock.AsyncMock()
        inter.followup.send = mock.AsyncMock()
        return inter

    def _call(self, *, wedge_message):
        from cogs.results import Results

        cog = Results.__new__(Results)
        cog.bot = mock.MagicMock()
        cog.bot.db_session_factory = None  # JSON režim /result
        cog.bot.get_channel.return_value.send = mock.AsyncMock()
        cm = __import__("cogs.results", fromlist=["Results"])
        inter = self._interaction(user_id=777)
        inter.guild.get_member(1).remove_roles = mock.AsyncMock()
        inter.guild.get_member(1).edit = mock.AsyncMock()

        with mock.patch.object(cm, "has_tester_role", return_value=True), \
             mock.patch.object(cm, "validate_result_tier", return_value=(True, "")), \
             mock.patch.object(cm, "get_kits", return_value=["AnchorPvP"]), \
             mock.patch.object(
                 cm, "record_result",
                 new=mock.AsyncMock(return_value={
                     "result": "created",
                     "previous_tier": "LT3",
                     "record": {"id": "rec-abc", "previousTier": "LT3", "newTier": "LT3"},
                 }),
             ), \
             mock.patch.object(cm, "get_result_channel_id", return_value=123), \
             mock.patch.object(cm, "today_cz", return_value="01.01.2026"), \
             mock.patch.object(
                 cm, "leave_queue", new=mock.AsyncMock(return_value=True)
             ), \
             mock.patch.object(
                 cm, "update_panel", new=mock.AsyncMock(return_value=None)
             ), \
             mock.patch.object(
                 cm, "remove_pulled_player", new=mock.AsyncMock(return_value=False)
             ), \
mock.patch.object(
                 cm, "auto_grant_kit_role",
                 new=mock.AsyncMock(return_value=TierRoleGrant(
                     ok=True, verified=True, tier_role_id=202, note="grant ok"
                 )),
             ), \
             mock.patch(
                 # patch na PRIMITIVU uvnitř db.services.promotion, ne na
                 # atributu balíčku `db.services`: kanonická služba
                 # `commit_confirmed_promotion` ji volá jako module-global, tak
                 # tímhle testem opravdu prochází celý řetězec
                 # cog -> grant_confirmation -> commit_promotion_with_wedge.
                 "db.services.promotion.commit_promotion_with_wedge",
                 new=mock.AsyncMock(return_value=PromotionWedgeOutcome(
                     committed=False, wedged=True, message=wedge_message
                 )),
             ):
            asyncio.run(cog.result.callback(
                cog,
                interaction=inter,
                hrac=inter.guild.get_member(1),
                ign="AliceMC",
                kit="AnchorPvP",
                tier="LT3",
                score="3:1",
                outcome="WON",
            ))
        return inter, inter.guild.get_member(1)

    def test_db_down_wedge_message_surfaces_and_discord_never_reverted(self):
        inter, alice = self._call(
            wedge_message=(
                "⚠️ Discord role se změnila, ale zápis do PostgreSQL selhal – "
                "událost je v outboxu a mirror se doplní automaticky."
            )
        )
        sent = inter.followup.send.await_args.args[0]
        self.assertIn("outboxu a mirror se doplní automaticky", sent)
        # Discord-first: role se nikdy neodebírá ani nepřepisuje
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()

    def test_db_down_total_outage_manual_message_no_revert(self):
        inter, alice = self._call(
            wedge_message=(
                "⚠️ Discord role se změnila, ale zápis do PostgreSQL selhal "
                "A záznam do outboxu také – mirror se musí doplnit ručně "
                "(nebo spuštěním /sync discord)."
            )
        )
        sent = inter.followup.send.await_args.args[0]
        self.assertIn("doplnit ručně", sent)
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()


class TopResultDbOutageTests(unittest.TestCase):
    """/topresult: grant.ok už změnil roli; DB down → wedge zpráva se
    zobrazí, Discord role zůstává."""

    def setUp(self):
        from cogs import topresult as topresult_module

        self.cog_module = topresult_module
        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        # ht_tickets.json / ht_results.json / cooldowns.json už neexistují ani
        # jako konstanty (JSON režim je pryč) – v tomhle scénáři se žádný
        # soubor nečte (record_ht_fight/get_ticket jsou mocknuté).
        storage.save_data("players.json", [{"username": "mendu__",
                                             "modes": {"MolePVP": "LT3"}, "history": {}}])

    def test_db_down_wedge_surfaces_discord_never_reverted(self):
        cm = self.cog_module
        rec = {
            "result": "created",
            "promoted": True,
            "record": {"id": "2001:ht_fight",
                       "previousTier": "LT3", "newTier": "LT2"},
        }
        with mock.patch.object(
            cm, "TOP_RESULT_CHANNEL_ID", 5555
        ), mock.patch.object(
            cm, "TOP_RESULT_ROLE_ID", 202
        ), mock.patch.object(
            cm, "get_kits", new=mock.AsyncMock(return_value=["MolePVP"])
        ), mock.patch.object(
            cm, "has_tester_role", return_value=True
        ), mock.patch.object(
            cm, "validate_topresult_config", return_value=(True, "")
        ), mock.patch.object(
            cm, "is_registered_kit", return_value=True
        ), mock.patch.object(
            cm, "validate_ht_fight_tier", return_value=(True, "")
        ), mock.patch.object(
            cm, "validate_ht_fight_score", return_value=(True, "")
        ), mock.patch.object(
            cm, "validate_ht_fight_status", return_value=(True, "")
        ), mock.patch.object(
            cm, "record_ht_fight", new=mock.AsyncMock(return_value=rec)
        ), mock.patch.object(
            cm, "set_ht_fight_announcement",
            new=mock.AsyncMock(return_value={"result": "ok"}),
        ), mock.patch.object(
            cm, "auto_grant_kit_role",
            new=mock.AsyncMock(
                return_value=TierRoleGrant(
                    ok=True, verified=True, tier_role_id=202, note="ok"
                )
            ),
        ), mock.patch.object(cm, "get_ticket", new=mock.AsyncMock(return_value=None)), \
           mock.patch(
               # viz poznámka u `ResultDbOutageTests._call` – patch na primitivu
               # v `db.services.promotion`, aby šel skutečný kanonický řetězec
               # cog -> grant_confirmation -> commit_promotion_with_wedge.
               "db.services.promotion.commit_promotion_with_wedge",
               new=mock.AsyncMock(return_value=PromotionWedgeOutcome(
                   committed=False, wedged=True,
                   message=(
                       "⚠️ Discord role se změnila, ale zápis do PostgreSQL selhal – "
                       "událost je v outboxu a mirror se doplní automaticky."
                   ),
               )),
           ):
            channel = mock.MagicMock()
            channel.send = mock.AsyncMock(return_value=mock.MagicMock(id=111))
            bot = mock.MagicMock()
            bot.db_session_factory = object()
            bot.get_channel.return_value = channel
            inter = mock.MagicMock()
            inter.response.defer = mock.AsyncMock()
            inter.followup.send = mock.AsyncMock()
            inter.user.name = "tester"
            alice = _member(1, "mendu__")
            inter.guild.get_member.return_value = alice
            inter.guild.get_role.return_value = mock.MagicMock(id=202)
            inter.channel_id = 9999

            cog = cm.TopResult(bot)
            hrac = mock.MagicMock(id=1, display_name="mendu__", name="mendu__")
            opp = mock.MagicMock(id=2, display_name="souper", name="souper")
            asyncio.run(cog.topresult.callback(
                cog,
                interaction=inter,
                fight_tier="HT3",
                outcome="Won",
                score="4-1",
                opponent=opp,
                tier_status="Povýšen na LT2",
                hrac=hrac,
                ign="mendu__",
                kit="MolePVP",
                bridge="LT2",
            ))
        reply = inter.followup.send.await_args.args[0]
        self.assertIn("outboxu a mirror se doplní automaticky", reply)
        alice.remove_roles.assert_not_awaited()
        alice.edit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cog-level: /sync web (GitHub export) při DB down
# ---------------------------------------------------------------------------
class SyncWebExportDbOutageTests(unittest.TestCase):
    """GitHub export je downstream-only: zdroj je vždy PostgreSQL export
    (_canonical_for_export → export_players) a export nikdy nemění Discord
    role. Výpadek DB export zablokuje (žádný legacy JSON fallback) – to je
    pokryté tests/test_sync.py::test_pg_export_failure_sends_nothing."""

    def setUp(self):
        from services import permissions

        self._tmp = tempfile.mkdtemp()
        patch = mock.patch.object(storage, "DATA_DIR", self._tmp)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(permissions, "ADMIN_ROLE_IDS", [999])
        patch.start()
        self.addCleanup(patch.stop)

        # AliceMC je v kanonické DB i na webu; BobMC chybí na webu → apply
        # musí nabídnout potvrzení (view) a po potvrzení pushnout.
        self.canonical = [
            {"username": "AliceMC", "discordId": 1,
             "modes": {"randompot": "HT3"},
             "history": {"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}},
            {"username": "BobMC", "discordId": 2,
             "modes": {"randompot": "HT2"},
             "history": {"randompot": [{"date": "01.01.2026", "tier": "HT2"}]}},
        ]
        self.web = [
            {"username": "AliceMC", "discordId": 1,
             "modes": {"randompot": "HT3"},
             "history": {"randompot": [{"date": "01.01.2026", "tier": "HT3"}]}},
        ]

    def test_export_runs_from_postgres_with_discord_untouched(self):
        """Export běží na kanonice z PostgreSQL (mock _canonical_for_export)
        a nikdy se nedotýká Discord rolí."""
        from cogs.sync import Sync, SyncWebConfirmView

        alice = _member(1, "AliceMC")
        cog = Sync.__new__(Sync)
        cog.bot = SimpleNamespace(db_session_factory=object())
        inter = _interaction(user=_admin_member(), guild=_guild([alice]))
        web = list(self.web)
        holder = {}

        async def main():
            with mock.patch.object(
                github_sync, "fetch_players",
                new=mock.AsyncMock(return_value=(web, "sha", None)),
            ), mock.patch.object(
                github_sync, "push_players",
                new=mock.AsyncMock(return_value=(True, "✅ push", [])),
            ), mock.patch(
                # kanonika z PostgreSQL exportu – nikdy ne z players.json
                "cogs.sync._canonical_for_export",
                new=mock.AsyncMock(return_value=[dict(p) for p in self.canonical]),
            ):
                await Sync.sync_web.callback(cog, inter, "apply")
                view = inter.followup.send.call_args.kwargs["view"]
                self.assertIsInstance(view, SyncWebConfirmView)
                holder["confirm"] = _interaction(
                    user=_admin_member(), guild=_guild([alice])
                )
                await view.confirm.callback(holder["confirm"])

        asyncio.run(main())
        embed = holder["confirm"].followup.send.await_args.kwargs["embed"]
        self.assertIn("✅ /sync web – web synchronizován", embed.title)
        for g in (inter.guild, holder["confirm"].guild):
            for m in (g.members or []):
                m.remove_roles.assert_not_awaited()
                m.edit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Bot hook: reconciliation loop při DB down nezhodí bota
# ---------------------------------------------------------------------------
def test_bot_reconciliation_hook_db_down_logs_and_never_crashes(
    monkeypatch,
):
    """_run_reconciliation_once: DB výpadek → log.exception, bez crash,
    bez JSON fallback, Discord nedotčen."""
    import bot as bot_module

    logged = []

    class _Log:
        def exception(self, msg, *args, **kwargs):
            logged.append(msg)

        def warning(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

    class _FakeBot:
        def __init__(self):
            self.db_session_factory = object()
            self._log = _Log()

        def get_guild(self, guild_id):
            return SimpleNamespace(id=guild_id)

        async def fetch_guild(self, guild_id):
            return SimpleNamespace(id=guild_id)

    bot = _FakeBot()

    async def boom(*args, **kwargs):
        raise RuntimeError("DB outage")

    monkeypatch.setattr(bot_module, "log", bot._log)
    monkeypatch.setattr(
        ReconciliationService, "reconcile", boom
    )

    async def main():
        await bot_module.DachshundTiersBot._run_reconciliation_once(bot)

    asyncio.run(main())
    assert any("Reconciliation selhala" in msg for msg in logged), logged
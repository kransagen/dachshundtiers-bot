"""F-FIX — JSON nesmí být autoritou pro current tier.

Regresní testy A–F z Phase F review:
  A) players.json má jiný current tier než Discord → "DB" v analýze je PG mirror
  B) players.json je nepřístupný → DB-backed operace fungují dál
  C) /sync discord / /sync check nemůžou změnit Discord role
  D) /sync discord zapisuje PG current tier jen z Discordu (ne z JSON)
  E) žádná cesta JSON → Discord
  F) žádná cesta JSON → authoritative PG current tier
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import select

import cogs.sync as sync_mod
import storage
from cogs.sync import SyncDiscordConfirmView, _canonical_players
from db.models import Kit, PlayerCurrentTier, TierDefinition
from db.repositories.kits import KitRoleRepository, ensure_dimensions
from db.repositories.players import PlayerRepository
from db.services.session import transaction
from services.checkweb import analyze_checkweb
from services.datacheck import perform_repairs
from services.phase_e.authority_scan import (
    AUTHORIZED_MUTATION_SITES,
    DISCORD_MUTATION_PATTERNS,
    build_authority_report,
    scan_patterns,
)

REPO = Path(__file__).resolve().parents[1]
ROLE_ID = 7777
IGN = "Divergent"

ROLE_MUTATION_TOKENS = ("add_roles", "remove_roles", ".edit(roles", "member.edit")


@dataclass(frozen=True)
class MemberView:
    id: int
    role_ids: tuple[int, ...]


@pytest.fixture
def pg_mode(tmp_path, monkeypatch, embedded_pg):
    """Aktivní PostgreSQL backend (storage.py JSONB blob store) + izolovaný
    DATA_DIR (repo se neznečistí).

    M6 audit fix: dřív se sem patchoval nerozlišitelný hostname
    ("postgresql://test/test"), který se nikde nedal přeložit — tyto testy
    ve skutečnosti vždy jen shodily RuntimeError před vlastními assercemi a
    nic neověřovaly. Teď se použije REÁLNÁ embedded Postgres instance (stejná
    jako pro `db/` vrstvu), aby test genuinně prošel storage.py connection
    kódem (`_ensure_postgres_schema` / `postgres_connection`).
    """
    from tests.conftest import _create_fresh_database, _sync_url

    db_name = f"pytest_phase_f_{uuid.uuid4().hex[:8]}"
    _create_fresh_database(embedded_pg, db_name)
    monkeypatch.setattr(storage, "DATABASE_URL", _sync_url(embedded_pg, db_name))
    # Module-level "schema created" flag — must reset per test DB, otherwise
    # a prior test's flag would skip CREATE TABLE against this fresh database.
    monkeypatch.setattr(storage, "_POSTGRES_SCHEMA_READY", False)
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    return tmp_path


async def _seed(session_factory, *, mirror_tier="t2", discord_tier="t3"):
    async with transaction(session_factory) as session:
        await ensure_dimensions(
            session,
            (("ht3", "HT3"),),
            (
                ("t1", "ladder", "Tier 1", 1),
                ("t2", "ladder", "Tier 2", 2),
                ("t3", "ladder", "Tier 3", 3),
            ),
        )
        kit = (await session.execute(select(Kit).where(Kit.key == "ht3"))).scalar_one()
        tiers = {}
        for code in ("t1", "t2", "t3"):
            tiers[code] = (
                await session.execute(
                    select(TierDefinition).where(TierDefinition.code == code)
                )
            ).scalar_one()
        await KitRoleRepository().set_mapping(
            session,
            kit_id=kit.id,
            tier_id=tiers[discord_tier].id,
            discord_role_id=ROLE_ID,
        )
        _, player = await PlayerRepository().claim_discord_id(
            session, discord_id=1111, ign=IGN
        )
        return {
            "kit": kit,
            "tiers": tiers,
            "player": player,
            "mirror_tier": mirror_tier,
            "discord_tier": discord_tier,
        }


async def _add_mirror_row(session_factory, seeded, tier_code):
    async with transaction(session_factory) as session:
        session.add(
            PlayerCurrentTier(
                player_id=seeded["player"].id,
                kit_id=seeded["kit"].id,
                tier_id=seeded["tiers"][tier_code].id,
                observed_at=datetime.now(timezone.utc),
                # M6 audit fix: "test" is not a valid `source` — the PG mirror's
                # CHECK constraint only allows 'discord_sync' | 'promotion' |
                # 'manual'. This helper simulates the PG mirror already
                # reflecting a confirmed Discord observation, so
                # "discord_sync" is the semantically correct value (this
                # previously never ran far enough to hit the constraint,
                # since the fixture's DB connection itself always failed).
                source="discord_sync",
            )
        )


async def _pg_tiers(session_factory, player_id, kit_id):
    async with transaction(session_factory) as session:
        rows = await session.execute(
            select(TierDefinition.code)
            .join(PlayerCurrentTier, PlayerCurrentTier.tier_id == TierDefinition.id)
            .where(
                PlayerCurrentTier.player_id == player_id,
                PlayerCurrentTier.kit_id == kit_id,
            )
        )
        return list(rows.scalars())


def _seed_json_shadow(tier):
    storage.save_data(
        "players.json",
        [
            {
                "username": IGN,
                "discordId": "1111",
                "modes": {"HT3": tier},
                "history": {},
            }
        ],
    )


def _json_shadow():
    return storage.load_data("players.json", []) or []


async def _analysis(session_factory, *, mirror_tier="t2", discord_tier="t3"):
    seeded = await _seed(
        session_factory, mirror_tier=mirror_tier, discord_tier=discord_tier
    )
    await _add_mirror_row(session_factory, seeded, mirror_tier)
    canonical = await _canonical_players(session_factory)
    analysis = analyze_checkweb(
        players=canonical,
        website=None,
        # analyze_checkweb matches Discord members to DB players by IGN
        # (`m["names"]`), NOT by "id" — this fixture previously only set
        # "id", so the member never matched and record["discord"] was
        # always empty (a pre-existing test bug this fixture never ran far
        # enough to hit before the M6 fix made pg_mode actually connect).
        members=[{"id": "1111", "names": [IGN], "role_ids": [ROLE_ID]}],
        roles_map={"ht3": {discord_tier.upper(): ROLE_ID}},
        kit_display={"ht3": "HT3"},
    )
    return seeded, canonical, analysis


def _ht3_record(analysis):
    return next(r for r in analysis["records"] if r.get("kit_key") == "ht3")


# ---------------------------------------------------------------------------
# A) players.json má jiný current tier než Discord → rozhoduje PG mirror
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_json_tier_never_wins_over_pg_mirror(session_factory, clean_db, pg_mode):
    _seed_json_shadow("t1")
    seeded, canonical, analysis = await _analysis(session_factory)

    assert canonical[0]["modes"]["HT3"] == "t2", "zdroj musí být PG mirror, ne JSON"

    record = _ht3_record(analysis)
    assert record["db"] == "T2", "sloupec db musí být PG mirror"
    assert record["discord"] == ["T3"]

    assert record["status"] == "DATABASE_MISMATCH"

    tiers = await _pg_tiers(session_factory, seeded["player"].id, seeded["kit"].id)
    assert tiers == ["t2"]


@pytest.mark.asyncio
async def test_a2_three_way_divergence_changes_nothing(
    session_factory, clean_db, pg_mode
):
    """JSON t1 / PG t2 / Discord t3 → tři různé hodnoty, žádná se nesmí uložit."""
    _seed_json_shadow("t1")
    seeded, _canonical, analysis = await _analysis(session_factory)

    record = _ht3_record(analysis)
    observed = {record["db"], _json_shadow()[0]["modes"]["HT3"].upper(), *record["discord"]}
    assert observed == {"T1", "T2", "T3"}
    assert _json_shadow()[0]["modes"]["HT3"] == "t1"
    tiers = await _pg_tiers(session_factory, seeded["player"].id, seeded["kit"].id)
    assert tiers == ["t2"]


# ---------------------------------------------------------------------------
# B) players.json nepřístupný → DB-backed operace fungují dál
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_b_checkweb_never_reads_json_in_db_mode(session_factory, clean_db, pg_mode):
    await _seed(session_factory)

    def _boom(*args, **kwargs):
        raise AssertionError("players.json nesmí být čten v PostgreSQL režimu")

    async def _members(_guild):
        return []

    cog = sync_mod.Sync(mock.Mock(db_session_factory=session_factory))
    inter = _interaction(session_factory)
    with (
        mock.patch.object(storage, "load_data", _boom),
        mock.patch.object(sync_mod, "guild_members", _members),
        mock.patch.object(sync_mod, "_fetch_website", _no_web),
        mock.patch.object(sync_mod, "log_checkweb_event", _audit_sink()),
        mock.patch.object(sync_mod, "_db_health_embed", _returns([])),
        mock.patch.object(sync_mod, "admin_gate_error", lambda _i: None),
    ):
        await cog._run_check(inter)

    inter.followup.send.assert_awaited()


@pytest.mark.asyncio
async def test_b2_no_legacy_players_writer_left_in_shared(pg_mode):
    """B2 (Phase B) byl „save_players v PG režimu odmítne" – dnes je už
    ``save_players`` z ``cogs/_shared.py`` úplně pryč (poslední legacy JSON
    zapisovatel bez produkčního volajícího). Aba zde je, že se nevrátí ani
    on, ani nahodilý ``tx.set("players.json", …)``."""
    import cogs._shared as shared_mod

    src = inspect.getsource(shared_mod)
    assert "def save_players" not in src
    assert 'tx.set("players.json"' not in src
    assert "using_postgres" not in src
    assert "services.store" not in src


@pytest.mark.asyncio
async def test_b3_perform_repairs_refuses_tier_normalization(pg_mode):
    result = await perform_repairs(
        tier_fixes=[{"username": IGN, "kit": "HT3", "field": "modes", "to": "T1"}]
    )
    assert result["ok"] is False
    assert result["normalized"] == []
    assert "player_current_tiers" in result["message"]


# ---------------------------------------------------------------------------
# C) /sync discord / /sync check nemůžou změnit Discord role
# ---------------------------------------------------------------------------
def test_c_views_have_no_role_mutation():
    src = inspect.getsource(SyncDiscordConfirmView)
    for token in ROLE_MUTATION_TOKENS:
        assert token not in src, f"SyncDiscordConfirmView nesmí mutovat role ({token})"


def test_c2_sync_cog_is_observe_only():
    src = (REPO / "cogs" / "sync.py").read_text(encoding="utf-8")
    for token in ROLE_MUTATION_TOKENS:
        assert token not in src, f"cogs/sync.py nesmí volat {token}"


def test_c3_mutation_surface_still_exactly_authorized():
    sites = scan_patterns(REPO, DISCORD_MUTATION_PATTERNS)
    # H5 audit fix: keyed by (module, qualname, pattern), not line number —
    # see services/phase_e/authority_scan.py.
    found = {(s["module"], s["qualname"], s["pattern"]) for s in sites}
    assert found == AUTHORIZED_MUTATION_SITES


# ---------------------------------------------------------------------------
# D) /sync discord zapisuje PG current tier jen z Discordu
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_d_sync_discord_writes_mirror_from_discord_not_json(
    session_factory, clean_db, pg_mode
):
    _seed_json_shadow("t1")
    seeded = await _seed(session_factory)
    await _add_mirror_row(session_factory, seeded, "t2")

    async def _members(_guild):
        return [MemberView(id=1111, role_ids=(ROLE_ID,))]

    view = SyncDiscordConfirmView()
    with (
        mock.patch.object(sync_mod, "guild_members", _members),
        mock.patch.object(sync_mod, "admin_gate_error", lambda _i: None),
    ):
        await view.confirm.callback(_interaction(session_factory))

    assert _json_shadow()[0]["modes"]["HT3"] == "t1", "JSONB stín se nesměl změnit"
    tiers = await _pg_tiers(session_factory, seeded["player"].id, seeded["kit"].id)
    assert tiers == ["t3"], "mirror následuje Discord, nikdy JSON"
    assert view.finished is True


def test_d2_no_persistence_left_in_views():
    src = inspect.getsource(SyncDiscordConfirmView)
    assert "save_players" not in src
    assert "sync_website" not in src
    assert "tx.set" not in src


# ---------------------------------------------------------------------------
# E) žádná cesta JSON → Discord
# ---------------------------------------------------------------------------
def test_e_no_json_to_discord_flow():
    report = build_authority_report(REPO)
    assert report["json_to_discord"] == []
    assert report["mutation_violations"] == []


# ---------------------------------------------------------------------------
# F) žádná cesta JSON → authoritative PG current tier
# ---------------------------------------------------------------------------
def test_f_pg_current_tier_only_written_from_db_layer():
    report = build_authority_report(REPO)
    assert report["pg_tier_violations"] == []
    assert all(s["module"].startswith("db.") for s in report["pg_tier_sites"])


@pytest.mark.asyncio
async def test_f2_repair_cannot_push_json_into_pg_mirror(
    session_factory, clean_db, pg_mode
):
    _seed_json_shadow("t1")
    seeded = await _seed(session_factory, mirror_tier="t2", discord_tier="t3")
    await _add_mirror_row(session_factory, seeded, "t2")

    result = await perform_repairs(
        tier_fixes=[{"username": IGN, "kit": "HT3", "field": "modes", "to": "T1"}]
    )

    assert result["ok"] is False
    tiers = await _pg_tiers(session_factory, seeded["player"].id, seeded["kit"].id)
    assert tiers == ["t2"]
    assert _json_shadow()[0]["modes"]["HT3"] == "t1"


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------
async def _no_web(*args, **kwargs):
    return [], "n/a", []


def _returns(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner


def _audit_sink():
    async def _inner(*args, **kwargs):
        return {"ok": True}

    return _inner


def _interaction(db_session_factory=None):
    inter = mock.Mock()
    inter.user = mock.Mock(id=42)
    inter.guild = mock.Mock(id=1)
    inter.message = mock.AsyncMock()
    inter.edit_original_response = mock.AsyncMock()
    inter.response = mock.AsyncMock()
    inter.followup = mock.AsyncMock()
    inter.client = mock.Mock(db_session_factory=db_session_factory)
    return inter

"""Phase B contract tests: no Discord access from db/, current tiers can
never drive Discord role mutations, PostgreSQL failure is loud (no JSON
fallback), and mirrors only track Discord-confirmed state."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.engine import create_async_engine_from_url, dispose_engine
from db.repositories.players import PlayerRepository
from db.services.session import transaction

REPO_ROOT = Path(__file__).resolve().parents[1]

BANNED_DISCORD_TOKENS = (
    "import discord",
    "from discord",
    "add_roles",
    "remove_roles",
    "fetch_member",
    "fetch_user",
    ".edit(roles",
    "discord.",
    "guild.",
)

BANNED_DISCORD_MUTATION_METHODS = (
    "add_role",
    "remove_role",
    "grant_role",
    "set_roles",
    "edit_roles",
    "role_write_plan",
    "apply_role_plan",
)


def _db_sources():
    return sorted((REPO_ROOT / "db").rglob("*.py"))


def test_db_package_never_imports_discord_import_graph():
    probes = [
        "import db.repositories",
        "import db.services",
        "import db",
    ]
    for probe in probes:
        script = (
            f"import sys; {probe}; "
            "print('discord' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        assert "False" in result.stdout, f"{probe} pulled discord into sys.modules"


def test_db_sources_contain_no_discord_mutation_tokens():
    for source in _db_sources():
        text = source.read_text(encoding="utf-8")
        for token in BANNED_DISCORD_TOKENS:
            assert token not in text, f"{source.name} contains {token!r}"


def test_db_sources_define_no_discord_mutation_api():
    for source in _db_sources():
        text = source.read_text(encoding="utf-8")
        for method in BANNED_DISCORD_MUTATION_METHODS:
            assert method not in text, f"{source.name} defines/uses {method!r}"


def test_repositories_have_no_clear_or_unobserve_apis():
    import db.repositories as repos

    mirror_violations = [
        name for name in dir(repos.MirrorRepository)
        if any(k in name for k in ("clear", "unobserve", "delete", "remove"))
    ]
    history_violations = [
        name for name in dir(repos.TierHistoryRepository)
        if name in ("update", "delete")
    ]
    assert mirror_violations == []
    assert history_violations == []


async def test_db_failure_raises_no_silent_json_fallback(migrated_db_url):
    dead = create_async_engine_from_url(
        "postgresql+asyncpg://postgres:@/pytest_phase_a?host=/nonexistent/socket",
    )
    try:
        dead_factory = async_sessionmaker(dead, expire_on_commit=False)
        with pytest.raises((DBAPIError, OSError)):
            async with transaction(dead_factory) as session:
                await PlayerRepository().list_all(session)
    finally:
        await dispose_engine(dead)


async def test_mirror_data_has_no_role_write_path(session_factory, clean_db):
    import inspect

    from db.repositories.tiers import MirrorRepository, MirrorServiceRepository

    for cls in (MirrorRepository, MirrorServiceRepository):
        for name, member in inspect.getmembers(cls):
            if name.startswith("_") or not callable(member):
                continue
            signature = inspect.signature(member)
            params = {p for p in signature.parameters}
            forbidden = params & {
                "discord_guild",
                "guild",
                "member",
                "bot",
                "client",
            }
            assert not forbidden, f"{cls.__name__}.{name} accepts {forbidden}"
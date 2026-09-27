"""Phase E, E12 — observability audit + secret scrub.

The phase constraint: never log/print DATABASE_URL, the DB password, the
Discord bot token, the GitHub token or OAuth secrets — neither as values nor
leaked through error propagation. Two independent lines of evidence:

1. Static AST audit of all production modules: no ``log.<level>(...)`` call,
   ``print(...)`` or ``raise <Error>(...)`` may *interpolate* a secret value
   (f-string ``{..}`` or a positional argument carrying a secret identifier).
   Names of the variables in plain text (e.g. "chybí GITHUB_TOKEN") are
   allowed — they carry no value and are required for actionable diagnostics.
2. Behavioural: the exact error paths an operator hits stay secret-free —
   ``storage.database_status()``, ``storage.postgres_connection()``,
   ``db.validation.validate_database()``, the GitHub fetch error surfaced to
   Discord, and the health report (JSON + rendered Markdown).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import storage
from db.services.health import build_health_report, render_markdown_report

REPO_ROOT = Path(__file__).resolve().parents[1]

FAKE_DB_URL = "postgresql://admin:S3cret!DBpass@127.0.0.1:1/dachshundtiers"
FAKE_DB_PASSWORD = "S3cret!DBpass"
FAKE_GITHUB_TOKEN = "ghp_fake_secret_value_42"

SECRET_IDENTIFIERS = {
    "DATABASE_URL",
    "DB_PASSWORD",
    "DISCORD_TOKEN",
    "BOT_TOKEN",
    "GITHUB_TOKEN",
    "OAUTH_TOKEN",
    "CLIENT_SECRET",
    "API_KEY",
    "API_SECRET",
    "TOKEN",
    "PASSWORD",
    "SECRET",
}
LOG_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}
LOGGER_NAMES = {"log", "logger"}


def _production_python_files():
    roots = [
        REPO_ROOT / "db",
        REPO_ROOT / "services",
        REPO_ROOT / "cogs",
        REPO_ROOT / "bot.py",
        REPO_ROOT / "config.py",
        REPO_ROOT / "storage.py",
        REPO_ROOT / "github_sync.py",
        REPO_ROOT / "migrate_json_to_postgres.py",
    ]
    for root in roots:
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))
        elif root.is_file():
            yield root


def _secret_identifiers_in(node) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id.upper() in SECRET_IDENTIFIERS:
            found.add(sub.id)
        elif (
            isinstance(sub, ast.Attribute)
            and sub.attr.upper() in SECRET_IDENTIFIERS
        ):
            found.add(sub.attr)
    return found


def _interpolated_secrets(node) -> set[str]:
    found: set[str] = set()
    if isinstance(node, ast.JoinedStr):
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                found |= _secret_identifiers_in(value.value)
    return found


def _call_secret_violations(args) -> list[str]:
    violations: list[str] = []
    for arg in args:
        if isinstance(arg, ast.Constant):
            continue
        if isinstance(arg, ast.JoinedStr):
            secrets = _interpolated_secrets(arg)
            if secrets:
                violations.append(
                    f"{sorted(secrets)} interpolováno ve f-string: {ast.unparse(arg)[:120]}"
                )
            continue
        secrets = _secret_identifiers_in(arg)
        if secrets:
            violations.append(
                f"{sorted(secrets)} předáno jako argument: {ast.unparse(arg)[:120]}"
            )
    return violations


def _audit_source(source: str, path: str) -> list[str]:
    tree = ast.parse(source, filename=path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in LOG_LEVELS
                and isinstance(func.value, ast.Name)
                and func.value.id in LOGGER_NAMES
            ):
                violations += [
                    f"{path}: log.{func.attr}(...) — {v}" for v in _call_secret_violations(node.args)
                ]
            elif isinstance(func, ast.Name) and func.id == "print":
                violations += [
                    f"{path}: print(...) — {v}" for v in _call_secret_violations(node.args)
                ]
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            violations += [
                f"{path}: raise ... — {v}" for v in _call_secret_violations(node.exc.args)
            ]
    return violations


def test_static_audit_no_secret_value_in_logs_or_raises():
    violations: list[str] = []
    for path in _production_python_files():
        violations += _audit_source(path.read_text(encoding="utf-8"), str(path))
    assert violations == [], "\n".join(violations)


def test_database_status_message_is_generic(caplog, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_URL", FAKE_DB_URL)
    status = storage.database_status()

    assert status["ok"] is False
    assert "postgresql://" not in json.dumps(status)
    assert FAKE_DB_PASSWORD not in json.dumps(status)
    assert (
        status["message"] == "PostgreSQL není dostupná; podrobnosti jsou v logu bota."
    )
    assert FAKE_DB_PASSWORD not in caplog.text
    assert FAKE_DB_URL not in caplog.text


def test_postgres_connection_error_is_generic(caplog, monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_URL", FAKE_DB_URL)
    with pytest.raises(RuntimeError) as excinfo:
        with storage.postgres_connection():
            pass  # pragma: no cover – connect fails before this line

    message = str(excinfo.value)
    assert "postgresql://" not in message
    assert FAKE_DB_PASSWORD not in message
    assert FAKE_DB_PASSWORD not in caplog.text
    assert FAKE_DB_URL not in caplog.text


async def test_validate_database_error_is_generic(caplog):
    from db.engine import create_async_engine_from_url, dispose_engine
    from db.validation import DatabaseConfigError, validate_database

    engine = create_async_engine_from_url(FAKE_DB_URL, poolclass=None)
    try:
        with pytest.raises(DatabaseConfigError) as excinfo:
            await validate_database(engine)
    finally:
        await dispose_engine(engine)

    message = str(excinfo.value)
    assert "postgresql://" not in message
    assert FAKE_DB_PASSWORD not in message
    assert FAKE_DB_PASSWORD not in caplog.text
    assert FAKE_DB_URL not in caplog.text


async def test_github_error_surface_omits_token(monkeypatch):
    import requests

    import github_sync

    monkeypatch.setattr(github_sync, "GITHUB_TOKEN", FAKE_GITHUB_TOKEN)

    def boom(*args, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(github_sync.requests, "get", boom)

    players, _sha, error = await github_sync.fetch_players()
    assert players is None
    assert error is not None
    surface = f"{error}"
    assert FAKE_GITHUB_TOKEN not in surface
    assert "Authorization" not in surface


async def test_health_report_and_markdown_secret_free(session_factory, clean_db):
    report = await build_health_report(session_factory)
    rendered = render_markdown_report(report)

    for output in (json.dumps(report, ensure_ascii=False), rendered):
        lowered = output.lower()
        assert "postgresql://" not in lowered
        assert "password" not in lowered
        assert "database_url" not in lowered
        assert "token" not in lowered
        assert "oauth" not in lowered
        assert "secret" not in lowered
        assert FAKE_DB_PASSWORD not in output
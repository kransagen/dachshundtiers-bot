"""Phase E, E1 — dual-write gate and watchdog divergence tests.

This module is DEAD as far as production is concerned: the G0 cutover audit
proved it has zero callers outside tests. The last test below is the guard
that keeps it quarantined — if someone wires it up, that test fails and the
design has to be revisited deliberately instead of the module quietly
becoming a second authority.
"""

from __future__ import annotations

import ast
import pathlib

import config

from services.phase_e.dual_write import json_export_enabled, watchdog_divergence

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_export_enabled_default():
    assert json_export_enabled() is True


def test_export_enabled_respects_flag(monkeypatch):
    monkeypatch.setattr(config, "PHASE_E_JSON_EXPORT_ENABLED", False)
    assert json_export_enabled() is False
    monkeypatch.setattr(config, "PHASE_E_JSON_EXPORT_ENABLED", True)
    assert json_export_enabled() is True


def test_watchdog_no_divergence():
    tiers = {"111": {"randompot": "HT3", "ironaxe": "LT3"}}
    report = watchdog_divergence(tiers, dict(tiers))
    assert report["divergent"] is False
    assert report["only_json"] == report["only_pg"] == report["mismatch"] == []


def test_watchdog_only_json():
    report = watchdog_divergence(
        {"111": {"randompot": "HT3"}},
        {"111": {}},
    )
    assert report["only_json"] == [("111", "randompot", "HT3")]
    assert report["divergent"] is True


def test_watchdog_only_pg():
    report = watchdog_divergence(
        {"111": {}},
        {"111": {"ironaxe": "LT3"}},
    )
    assert report["only_pg"] == [("111", "ironaxe", "LT3")]
    assert report["divergent"] is True


def test_watchdog_mismatch():
    report = watchdog_divergence(
        {"111": {"randompot": "HT3"}},
        {"111": {"randompot": "LT3"}},
    )
    assert report["mismatch"] == [("111", "randompot", "HT3", "LT3")]
    assert report["divergent"] is True


def test_watchdog_is_pure_observation():
    json_tiers = {"111": {"randompot": "HT3"}}
    pg_tiers = {"111": {"randompot": "LT3"}}
    snapshot = (dict(json_tiers), dict(pg_tiers))
    watchdog_divergence(json_tiers, pg_tiers)
    assert (json_tiers, pg_tiers) == snapshot


def test_dual_write_module_has_no_production_callers():
    """D8 quarantine guard.

    `services/phase_e/dual_write.py` is proven-unused in production, so it is
    documented as deferred rather than deleted (it is an inert, pure design
    deliverable that cannot influence a promotion). This test makes that
    documentation enforceable: nothing outside the tests may import the
    module or reference its two functions.
    """
    module_needles = ("phase_e.dual_write", "phase_e/dual_write", "dual_write")
    function_names = {"json_export_enabled", "watchdog_divergence"}
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] == "tests" or str(rel) == "services/phase_e/dual_write.py":
            continue  # the tests and the definitions themselves are allowed
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{rel}: import {alias.name}"
                    for alias in node.names
                    if any(n in alias.name for n in module_needles)
                ]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if any(n in module for n in module_needles):
                    offenders.append(f"{rel}: from {module} import ...")
                offenders += [
                    f"{rel}: import {alias.name}"
                    for alias in node.names
                    if alias.name in function_names
                ]
            elif (
                isinstance(node, ast.Name)
                and node.id in function_names
                and not isinstance(node.ctx, ast.Store)
                and not isinstance(node.ctx, ast.Del)
            ):
                offenders.append(f"{rel}: reference {node.id}")
    assert offenders == [], (
        "services/phase_e/dual_write.py gained a production caller; it is "
        f"quarantined as dead code: {offenders}"
    )
"""Phase E, E1 — dual-write gate and watchdog divergence tests."""

from __future__ import annotations

import config

from services.phase_e.dual_write import json_export_enabled, watchdog_divergence


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
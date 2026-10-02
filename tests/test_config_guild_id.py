"""GUILD_ID z prostředí: prázdné = globální režim, nečíselné = chyba startu."""

import importlib

import pytest

import config


@pytest.fixture(autouse=True)
def _reload_config_after(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(config)


def test_guild_id_valid(monkeypatch):
    monkeypatch.setenv("GUILD_ID", " 123 ")
    assert config._guild_id_env() == 123


@pytest.mark.parametrize("value", ["", "0"])
def test_guild_id_unset_is_none(monkeypatch, value):
    monkeypatch.setenv("GUILD_ID", value)
    assert config._guild_id_env() is None


def test_guild_id_invalid_aborts_startup(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "12x4")
    with pytest.raises(SystemExit) as excinfo:
        config._guild_id_env()
    assert "GUILD_ID" in str(excinfo.value)

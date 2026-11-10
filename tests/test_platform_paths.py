"""Where files live: XDG folders on Linux and macOS, %LOCALAPPDATA% on Windows (the platform is switched in the
test, so the Windows branch runs everywhere; on a real Windows machine it is also the one that runs unswitched)."""
from __future__ import annotations

import sys

import pytest

from resonant_archive import commands, config
from resonant_archive.embed import paths as embed_paths


@pytest.fixture
def windows(monkeypatch, tmp_path):
    local = tmp_path / "Local"
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(embed_paths.sys, "platform", "win32")
    monkeypatch.setattr(commands.fsutil, "WINDOWS", True)
    return local


def test_windows_uses_localappdata_for_config_data_and_state(windows):
    assert config.config_dir() == windows / "resonant-archive" / "config"
    assert config.data_dir() == windows / "resonant-archive"
    assert config.state_dir() == windows / "resonant-archive" / "state"
    assert config.config_file() == windows / "resonant-archive" / "config" / "config.toml"


def test_the_embedder_and_the_cli_agree_on_the_windows_data_folder(windows):
    assert embed_paths.data_dir() == config.data_dir()
    assert embed_paths.model_dir() == config.data_dir() / "models" / embed_paths.MODEL_DIRNAME
    assert commands.default_project_dir() == config.data_dir() / "docker"


def test_an_absolute_xdg_variable_still_wins_on_windows(windows, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config.config_dir() == tmp_path / "cfg" / "resonant-archive"


def test_windows_keeps_using_the_old_dot_config_folder_until_the_new_one_exists(windows, monkeypatch, tmp_path):
    home = tmp_path / "oldhome"
    old = home / ".config" / "resonant-archive"
    old.mkdir(parents=True)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: home))
    assert config.config_dir() == old
    (windows / "resonant-archive" / "config").mkdir(parents=True)
    assert config.config_dir() == windows / "resonant-archive" / "config"


def test_windows_keeps_using_an_existing_docker_project_in_the_old_place(windows, monkeypatch, tmp_path):
    home = tmp_path / "oldhome"
    old = home / ".local" / "share" / "resonant-archive" / "docker"
    old.mkdir(parents=True)
    monkeypatch.setattr(commands.Path, "home", classmethod(lambda cls: home))
    assert commands.default_project_dir() == old


@pytest.mark.skipif(sys.platform == "win32", reason="the Linux branch")
def test_linux_still_uses_the_xdg_defaults(monkeypatch, tmp_path):
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.config_dir() == tmp_path / ".config" / "resonant-archive"
    assert config.data_dir() == tmp_path / ".local" / "share" / "resonant-archive"

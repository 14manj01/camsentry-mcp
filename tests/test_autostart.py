from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from camsentry_mcp import autostart

COMMAND = ["/opt/py thon/bin/python", "-m", "camsentry_mcp", "watch"]


def test_windows_entry_is_a_startup_folder_script_that_survives_spaces_in_paths(tmp_path: Path) -> None:
    command = [r"C:\Program Files\Py\pythonw.exe", "-m", "camsentry_mcp", "watch"]
    path, content = autostart.entry("win32", {"APPDATA": str(tmp_path)}, tmp_path, command)
    assert path == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "camsentry-mcp-watch.cmd"
    assert content.decode().splitlines() == [
        "@echo off",
        r'start "" "C:\Program Files\Py\pythonw.exe" "-m" "camsentry_mcp" "watch"',
    ]


def test_macos_entry_is_a_launch_agent_that_runs_the_watcher_at_login(tmp_path: Path) -> None:
    path, content = autostart.entry("darwin", {}, tmp_path, COMMAND)
    assert path == tmp_path / "Library" / "LaunchAgents" / "io.github.camsentry-mcp.watch.plist"
    agent = plistlib.loads(content)
    assert agent["ProgramArguments"] == COMMAND
    assert agent["RunAtLoad"] is True
    assert agent["Label"] == path.stem


def test_linux_entry_is_a_desktop_autostart_file_with_a_quoted_command(tmp_path: Path) -> None:
    path, content = autostart.entry("linux", {"XDG_CONFIG_HOME": str(tmp_path / "cfg")}, tmp_path, COMMAND)
    assert path == tmp_path / "cfg" / "autostart" / "camsentry-mcp-watch.desktop"
    lines = content.decode().splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert "Exec='/opt/py thon/bin/python' -m camsentry_mcp watch" in lines


def test_linux_entry_defaults_to_the_users_config_folder(tmp_path: Path) -> None:
    path, _ = autostart.entry("linux", {}, tmp_path, COMMAND)
    assert path.parent == tmp_path / ".config" / "autostart"


def test_turning_autostart_on_and_off_adds_and_removes_exactly_one_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "startup" / "entry"
    monkeypatch.setattr(autostart, "_here", lambda: (target, b"run the watcher"))

    assert autostart.is_enabled() is False
    assert autostart.enable() == target
    assert target.read_bytes() == b"run the watcher"
    assert autostart.is_enabled() is True

    assert autostart.disable() is True
    assert not target.exists()
    assert autostart.disable() is False

"""Start the watcher when the user logs in.

Each platform has a per-user folder whose entries run at login. Turning autostart on writes one
small file there; turning it off deletes that file. No administrator rights, no system service.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_NAME = "camsentry-mcp-watch"


def watch_command() -> list[str]:
    """The command that runs the watcher, without a console window where the platform allows it."""
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.exists():
        python = windowless
    return [str(python), "-m", "camsentry_mcp", "watch"]


def entry(platform: str, env: Mapping[str, str], home: Path, command: Sequence[str]) -> tuple[Path, bytes]:
    """The autostart file for one platform: where it goes and what it contains."""
    if platform == "win32":
        roaming = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        folder = roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        line = 'start "" ' + " ".join(f'"{part}"' for part in command)
        return folder / f"{_NAME}.cmd", f"@echo off\r\n{line}\r\n".encode()
    if platform == "darwin":
        label = "io.github.camsentry-mcp.watch"
        agent = {"Label": label, "ProgramArguments": list(command), "RunAtLoad": True, "ProcessType": "Background"}
        return home / "Library" / "LaunchAgents" / f"{label}.plist", plistlib.dumps(agent)
    config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    desktop = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=camsentry-mcp watch\n"
        "Comment=Watch Wyze cameras and send alerts\n"
        f"Exec={shlex.join(command)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    return config / "autostart" / f"{_NAME}.desktop", desktop.encode()


def _here() -> tuple[Path, bytes]:
    return entry(sys.platform, os.environ, Path.home(), watch_command())


def enable() -> Path:
    path, content = _here()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def disable() -> bool:
    """Remove the autostart file. Returns whether there was one."""
    path, _ = _here()
    if not path.exists():
        return False
    path.unlink()
    return True


def is_enabled() -> bool:
    return _here()[0].exists()

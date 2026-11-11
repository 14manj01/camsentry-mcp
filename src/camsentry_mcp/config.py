"""Non-secret settings, read from environment variables. Every one of them is optional.

Secrets and contact details never come from here: they live in the OS keychain (credentials.py).
"""

from __future__ import annotations

import ipaddress
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_VISION_URL = "http://127.0.0.1:11434/v1"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_POLL_SECONDS = 60
MIN_POLL_SECONDS = 15

# Every alert the watcher can raise, most important first.
ALERT_TYPES = ("intruder", "package_taken", "lingering", "person", "package", "rule", "vehicle", "pet")
# Pets and vehicles are recorded but stay quiet unless asked for: they are rarely worth a text,
# and on their own they do not make a visit notable.
QUIET_ALERTS = frozenset({"vehicle", "pet"})
DEFAULT_NOTIFY = frozenset(ALERT_TYPES) - QUIET_ALERTS


class ConfigError(ValueError):
    """A setting is malformed or violates a safety rule."""


@dataclass(frozen=True)
class ArmedHours:
    """Daily local-time window during which any person counts as an intruder."""

    start: time
    end: time

    @classmethod
    def parse(cls, text: str) -> ArmedHours:
        try:
            start_text, end_text = text.split("-")
            return cls(time.fromisoformat(start_text.strip()), time.fromisoformat(end_text.strip()))
        except ValueError:
            raise ConfigError(f"CAMSENTRY_ARMED_HOURS must look like '22:00-06:00', got {text!r}") from None

    def contains(self, moment: time) -> bool:
        if self.start < self.end:
            return self.start <= moment < self.end
        # Window wraps past midnight; start == end means the whole day.
        return moment >= self.start or moment < self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def is_loopback_url(url: str) -> bool:
    host = urlsplit(url).hostname
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _http_url(name: str, value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{name} must be an http(s) URL, got {value!r}")
    if parts.username or parts.password:
        raise ConfigError(f"{name} must not embed credentials in the URL")
    return value.rstrip("/")


def _notify_types(text: str | None) -> frozenset[str]:
    if text is None:
        return DEFAULT_NOTIFY
    chosen = frozenset(part.strip().lower() for part in text.split(",") if part.strip())
    unknown = chosen - set(ALERT_TYPES)
    if unknown:
        raise ConfigError(
            f"CAMSENTRY_NOTIFY has unknown alert types {sorted(unknown)}. Choose from: {', '.join(ALERT_TYPES)}."
        )
    return chosen


def _poll_seconds(text: str | None) -> int:
    if text is None:
        return DEFAULT_POLL_SECONDS
    try:
        seconds = int(text)
    except ValueError:
        raise ConfigError(f"CAMSENTRY_POLL_SECONDS must be a whole number, got {text!r}") from None
    if seconds != 0 and seconds < MIN_POLL_SECONDS:
        raise ConfigError(f"CAMSENTRY_POLL_SECONDS must be 0 (off) or at least {MIN_POLL_SECONDS}.")
    return seconds


def default_data_dir(env: Mapping[str, str]) -> Path:
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "camsentry-mcp"


@dataclass(frozen=True)
class Settings:
    vision_url: str
    vision_model: str | None  # None: pick an installed vision model automatically
    armed_hours: ArmedHours | None
    notify_types: frozenset[str]
    poll_seconds: int  # 0 turns background watching off
    ntfy_server: str
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        vision_url = _http_url("CAMSENTRY_VISION_URL", env.get("CAMSENTRY_VISION_URL") or DEFAULT_VISION_URL)
        if not is_loopback_url(vision_url) and env.get("CAMSENTRY_ALLOW_REMOTE_VISION") != "1":
            raise ConfigError(
                f"CAMSENTRY_VISION_URL ({vision_url}) is not on this machine. Camera images would leave "
                "this computer. Set CAMSENTRY_ALLOW_REMOTE_VISION=1 only if you trust that host."
            )
        hours = env.get("CAMSENTRY_ARMED_HOURS")
        return cls(
            vision_url=vision_url,
            vision_model=env.get("CAMSENTRY_VISION_MODEL") or None,
            armed_hours=ArmedHours.parse(hours) if hours else None,
            notify_types=_notify_types(env.get("CAMSENTRY_NOTIFY")),
            poll_seconds=_poll_seconds(env.get("CAMSENTRY_POLL_SECONDS")),
            ntfy_server=_http_url("CAMSENTRY_NTFY_SERVER", env.get("CAMSENTRY_NTFY_SERVER") or DEFAULT_NTFY_SERVER),
            data_dir=Path(env["CAMSENTRY_DATA_DIR"]) if env.get("CAMSENTRY_DATA_DIR") else default_data_dir(env),
        )

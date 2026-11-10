"""Non-secret settings, read from environment variables.

Secrets never come from here: Wyze tokens live in the OS keychain (see credentials.py).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from urllib.parse import urlsplit

DEFAULT_VISION_URL = "http://127.0.0.1:11434/v1"
DEFAULT_VISION_MODEL = "gemma3:4b"


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
            raise ConfigError(f"WYZE_CAM_ARMED_HOURS must look like '22:00-06:00', got {text!r}") from None

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


@dataclass(frozen=True)
class Settings:
    vision_url: str
    vision_model: str
    bridge_url: str | None
    armed_hours: ArmedHours | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        vision_url = _http_url("WYZE_CAM_VISION_URL", env.get("WYZE_CAM_VISION_URL") or DEFAULT_VISION_URL)
        if not is_loopback_url(vision_url) and env.get("WYZE_CAM_ALLOW_REMOTE_VISION") != "1":
            raise ConfigError(
                f"WYZE_CAM_VISION_URL ({vision_url}) is not on this machine. Camera images would leave "
                "this computer. Set WYZE_CAM_ALLOW_REMOTE_VISION=1 only if you trust that host."
            )
        bridge = env.get("WYZE_CAM_BRIDGE_URL")
        hours = env.get("WYZE_CAM_ARMED_HOURS")
        return cls(
            vision_url=vision_url,
            vision_model=env.get("WYZE_CAM_VISION_MODEL") or DEFAULT_VISION_MODEL,
            bridge_url=_http_url("WYZE_CAM_BRIDGE_URL", bridge) if bridge else None,
            armed_hours=ArmedHours.parse(hours) if hours else None,
        )

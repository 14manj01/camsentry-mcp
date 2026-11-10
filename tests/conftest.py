from __future__ import annotations

from datetime import datetime

import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from camsentry_mcp.credentials import SecretStore
from camsentry_mcp.images import ImageError
from camsentry_mcp.vision import Detection, VisionError
from camsentry_mcp.wyze import Camera, CameraEvent

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.entries:
            raise PasswordDeleteError(username)
        del self.entries[(service, username)]


@pytest.fixture
def keyring_backend() -> MemoryKeyring:
    return MemoryKeyring()


@pytest.fixture
def store(keyring_backend: MemoryKeyring) -> SecretStore:
    return SecretStore(keyring_backend)


def at(text: str) -> datetime:
    """A timezone-aware local time such as at("2026-03-01 23:30")."""
    return datetime.fromisoformat(text).astimezone()


def event(event_id: str, when: str, *, mac: str = "MAC1", tags: tuple[str, ...] = (), image: bool = True) -> CameraEvent:
    return CameraEvent(
        id=event_id,
        mac=mac,
        time=at(when),
        kind="Motion",
        wyze_tags=tags,
        image_url=f"https://img.example/{event_id}.jpg" if image else None,
    )


class FakeWyze:
    """Stands in for WyzeCloud: serves whatever events are currently in `self.all_events`."""

    def __init__(self, cameras: list[Camera], events: list[CameraEvent] | None = None) -> None:
        self._cameras = cameras
        self.all_events = list(events or [])

    def cameras(self) -> list[Camera]:
        return self._cameras

    def events(self, *, since, until, macs=(), limit=20, oldest_first=False):
        matching = [
            e for e in self.all_events if since <= e.time <= until and (not macs or e.mac in macs)
        ]
        return sorted(matching, key=lambda e: e.time, reverse=not oldest_first)[:limit]

    def event_image(self, e: CameraEvent) -> tuple[bytes, str]:
        if e.image_url is None:
            raise ImageError("That event has no image.")
        return JPEG, "image/jpeg"


class FakeDetector:
    def __init__(self, result: Detection | None = None, error: str | None = None) -> None:
        self.result = result or Detection(0, (), 0, False, "", "Empty porch.")
        self.error = error
        self.calls = 0

    def detect(self, image: bytes, mime: str) -> Detection:
        self.calls += 1
        if self.error:
            raise VisionError(self.error)
        return self.result


class FakeBridge:
    def snapshot(self, nickname: str) -> tuple[bytes, str]:
        return JPEG, "image/jpeg"

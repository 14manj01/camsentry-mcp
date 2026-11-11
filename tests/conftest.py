from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from camsentry_mcp.credentials import SecretStore
from camsentry_mcp.images import ImageError
from camsentry_mcp.notify import Notifier
from camsentry_mcp.policy import ArmState
from camsentry_mcp.config import DEFAULT_NOTIFY, ArmedHours
from camsentry_mcp.service import CameraWatch
from camsentry_mcp.store import Store
from camsentry_mcp.vision import Detection, VisionError
from camsentry_mcp.wyze import Camera, CameraEvent

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
CAMERAS = [Camera("MAC1", "Front Door", "HL_CAM4", True), Camera("MAC2", "Backyard", "ME_WCO3", True)]


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
    """The keychain-backed secret store."""
    return SecretStore(keyring_backend)


@pytest.fixture
def memory(tmp_path: Path):
    """The on-disk memory of analysed stills."""
    db = Store(tmp_path / "camsentry.db")
    yield db
    db.close()


def at(text: str) -> datetime:
    """A timezone-aware local time such as at("2026-03-01 23:30")."""
    return datetime.fromisoformat(text).astimezone()


def seen(people: int = 0, pets: tuple[str, ...] = (), vehicles: int = 0, packages: int = 0, suspicious: bool = False,
         reason: str = "", summary: str = "A quiet scene.") -> Detection:
    return Detection(people, pets, vehicles, packages, suspicious, reason, summary)


def event(event_id: str, when: str, *, mac: str = "MAC1", tags: tuple[str, ...] = (), image: bool = True) -> CameraEvent:
    return CameraEvent(
        id=event_id,
        mac=mac,
        time=at(when),
        kind="Motion",
        wyze_tags=tags,
        image_url=f"https://img.example/{event_id}.jpg" if image else None,
    )


class Clock:
    def __init__(self, start: str) -> None:
        self.now = at(start)

    def __call__(self) -> datetime:
        return self.now

    def set(self, text: str) -> None:
        self.now = at(text)


class FakeWyze:
    """Stands in for WyzeCloud: serves whatever events are currently in `self.all_events`."""

    def __init__(self, events: list[CameraEvent] | None = None) -> None:
        self.all_events = list(events or [])
        self.broken_images: set[str] = set()

    def cameras(self) -> list[Camera]:
        return CAMERAS

    def events(self, *, since, until, macs=(), limit=20, oldest_first=False):
        matching = [e for e in self.all_events if since <= e.time <= until and (not macs or e.mac in macs)]
        return sorted(matching, key=lambda e: e.time, reverse=not oldest_first)[:limit]

    def event_image(self, e: CameraEvent) -> tuple[bytes, str]:
        if e.image_url is None or e.id in self.broken_images:
            raise ImageError("The image request returned HTTP 403.")
        return JPEG, "image/jpeg"


class FakeDetector:
    """Answers each still with the next entry of `script`, else with `result`.

    A custom statement is judged true when it is in `true_statements` for the still being analysed
    (keyed by its position: 1 for the first still, 2 for the second, ...).
    """

    def __init__(self, result: Detection | None = None) -> None:
        self.result = result or seen()
        self.error: VisionError | None = None
        self.calls = 0
        self.script: list[Detection] = []
        self.asked: list[list[str]] = []
        self.true_statements: dict[int, set[str]] = {}

    def detect(self, image: bytes, mime: str) -> Detection:
        if self.error:
            raise self.error
        self.calls += 1
        return self.script.pop(0) if self.script else self.result

    def check(self, image: bytes, mime: str, statements) -> tuple[bool, ...]:
        self.asked.append(list(statements))
        true_now = self.true_statements.get(self.calls, set())
        return tuple(statement in true_now for statement in statements)


class Outbox:
    """An httpx transport that records notification requests and answers as the services would."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.fail:
            return httpx.Response(503)
        if request.url.host == "textbelt.com":
            return httpx.Response(200, json={"success": True, "quotaRemaining": 40})
        return httpx.Response(200, json={"id": "x"})

    def bodies(self, host: str) -> list[dict]:
        import json

        return [json.loads(r.content) for r in self.requests if r.url.host == host]


class Rig:
    """A CameraWatch wired to fakes, plus handles on every fake."""

    def __init__(self, memory: Store, secrets: SecretStore, events=(), *, hours: str | None = None,
                 now: str = "2026-03-01 12:00", notify=DEFAULT_NOTIFY) -> None:
        self.wyze = FakeWyze(list(events))
        self.detector = FakeDetector()
        self.clock = Clock(now)
        self.outbox = Outbox()
        self.secrets = secrets
        self.memory = memory
        self._hours, self._notify = hours, notify
        self.watch = self.restart()

    def restart(self) -> CameraWatch:
        """A fresh CameraWatch over the same store, as after the server process restarts."""
        notifier = Notifier(self.secrets, httpx.Client(transport=httpx.MockTransport(self.outbox)), "https://ntfy.example")
        arm = ArmState(ArmedHours.parse(self._hours) if self._hours else None, self.memory)
        self.watch = CameraWatch(self.wyze, self.detector, arm, self.memory, notifier, self._notify, clock=self.clock)
        return self.watch


@pytest.fixture
def rig(memory: Store, store: SecretStore):
    def build(events=(), **options) -> Rig:
        return Rig(memory, store, events, **options)

    return build

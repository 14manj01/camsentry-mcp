"""The camera-watching logic behind the MCP tools. Knows nothing about MCP."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .bridge import LiveBridge
from .images import ImageError
from .policy import ArmMode, ArmState, build_alerts
from .vision import Detection, VisionDetector, VisionError
from .wyze import Camera, CameraEvent, WyzeCloud

INITIAL_LOOKBACK = timedelta(minutes=15)
# Wyze uploads events some time after they start, so each poll re-reads a little history.
_OVERLAP = timedelta(minutes=5)
_LATEST_IMAGE_WINDOW = timedelta(hours=24)
_CAMERA_CACHE_SECONDS = 60
_REMEMBERED_EVENTS = 2000
_WATCH_FETCH = 100
_ALERT_TYPES = ("person", "pet", "package", "intruder")


class WatchError(Exception):
    """A request cannot be satisfied; the message is safe to show to the model."""


def local_now() -> datetime:
    return datetime.now().astimezone()


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


class CameraWatch:
    def __init__(
        self,
        wyze: WyzeCloud,
        detector: VisionDetector,
        arm: ArmState,
        live: LiveBridge | None = None,
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self._wyze = wyze
        self._detector = detector
        self._arm = arm
        self._live = live
        self._clock = clock
        self._state_lock = threading.Lock()
        self._watch_lock = threading.Lock()
        self._cameras: list[Camera] = []
        self._cameras_at: datetime | None = None
        self._events: OrderedDict[str, CameraEvent] = OrderedDict()
        self._reported: OrderedDict[str, None] = OrderedDict()
        self._cursor: datetime | None = None

    # -- lookups -----------------------------------------------------------

    def _camera_list(self) -> list[Camera]:
        now = self._clock()
        with self._state_lock:
            fresh = self._cameras_at is not None and (now - self._cameras_at).total_seconds() < _CAMERA_CACHE_SECONDS
            if fresh:
                return self._cameras
        cameras = self._wyze.cameras()
        with self._state_lock:
            self._cameras, self._cameras_at = cameras, now
        return cameras

    def _resolve(self, camera: str) -> Camera:
        wanted = camera.strip().casefold()
        cameras = self._camera_list()
        matches = [cam for cam in cameras if wanted in (cam.name.casefold(), cam.mac.casefold())]
        if len(matches) == 1:
            return matches[0]
        if matches:
            macs = ", ".join(cam.mac for cam in matches)
            raise WatchError(f"Several cameras are named {camera!r}. Use one of these ids instead: {macs}.")
        names = ", ".join(sorted(cam.name for cam in cameras)) or "none"
        raise WatchError(f"No camera named {camera!r}. Available cameras: {names}.")

    def _remember(self, events: list[CameraEvent]) -> None:
        with self._state_lock:
            for event in events:
                self._events[event.id] = event
                self._events.move_to_end(event.id)
            while len(self._events) > _REMEMBERED_EVENTS:
                self._events.popitem(last=False)

    def _find_event(self, camera: Camera, event_id: str) -> CameraEvent:
        with self._state_lock:
            event = self._events.get(event_id)
        if event is None:
            now = self._clock()
            recent = self._wyze.events(since=now - _LATEST_IMAGE_WINDOW, until=now, macs=[camera.mac], limit=100)
            self._remember(recent)
            event = next((candidate for candidate in recent if candidate.id == event_id), None)
        if event is None or event.mac != camera.mac:
            raise WatchError(
                f"No event {event_id!r} on camera {camera.name!r} in the last 24 hours. "
                "Use get_events to list valid event ids."
            )
        return event

    def _frame(self, camera: Camera, event_id: str | None) -> tuple[CameraEvent | None, datetime, bytes, str]:
        """Pick the image to look at: a named event, else a live frame, else the latest event image."""
        if event_id is not None:
            event = self._find_event(camera, event_id)
        elif self._live is not None:
            data, mime = self._live.snapshot(camera.name)
            return None, self._clock(), data, mime
        else:
            now = self._clock()
            recent = self._wyze.events(since=now - _LATEST_IMAGE_WINDOW, until=now, macs=[camera.mac])
            self._remember(recent)
            event = next((candidate for candidate in recent if candidate.image_url), None)
            if event is None:
                raise WatchError(
                    f"Camera {camera.name!r} has no event image from the last 24 hours. A live view "
                    "needs a wyze-bridge (CAMSENTRY_BRIDGE_URL)."
                )
        data, mime = self._wyze.event_image(event)
        return event, event.time, data, mime

    # -- output shaping ----------------------------------------------------

    def _report(
        self,
        camera_name: str,
        event: CameraEvent | None,
        when: datetime,
        detection: Detection | None,
        vision_error: str | None = None,
    ) -> dict[str, Any]:
        armed = self._arm.is_armed(when)
        tags = event.wyze_tags if event else ()
        report: dict[str, Any] = {
            "camera": camera_name,
            "source": "event" if event else "live",
            "event_id": event.id if event else None,
            "time": _iso(when),
            "wyze_tags": list(tags),
            "armed": armed,
            "detection": detection.to_dict() if detection else None,
            "alerts": build_alerts(detection, tags, armed),
        }
        if vision_error:
            report["vision_error"] = vision_error
        return report

    # -- tools -------------------------------------------------------------

    def list_cameras(self) -> dict[str, Any]:
        return {
            "cameras": [
                {"name": cam.name, "id": cam.mac, "model": cam.model, "online": cam.online}
                for cam in self._camera_list()
            ],
            "live_view": self._live is not None,
            "arming": self._arm.describe(self._clock()),
        }

    def get_events(self, camera: str | None, minutes: int, limit: int) -> dict[str, Any]:
        cameras = self._camera_list()
        macs = [self._resolve(camera).mac] if camera else []
        names = {cam.mac: cam.name for cam in cameras}
        now = self._clock()
        events = self._wyze.events(since=now - timedelta(minutes=minutes), until=now, macs=macs, limit=limit)
        self._remember(events)
        return {
            "events": [
                {
                    "event_id": event.id,
                    "camera": names.get(event.mac, event.mac),
                    "time": _iso(event.time),
                    "kind": event.kind,
                    "wyze_tags": list(event.wyze_tags),
                    "has_image": event.image_url is not None,
                }
                for event in events
            ]
        }

    def view(self, camera: str, event_id: str | None) -> tuple[dict[str, Any], bytes, str]:
        cam = self._resolve(camera)
        event, when, data, mime = self._frame(cam, event_id)
        meta = {
            "camera": cam.name,
            "source": "event" if event else "live",
            "event_id": event.id if event else None,
            "time": _iso(when),
        }
        return meta, data, mime

    def analyze(self, camera: str, event_id: str | None) -> dict[str, Any]:
        cam = self._resolve(camera)
        event, when, data, mime = self._frame(cam, event_id)
        return self._report(cam.name, event, when, self._detector.detect(data, mime))

    def watch(self, max_events: int) -> dict[str, Any]:
        with self._watch_lock:
            now = self._clock()
            since = self._cursor - _OVERLAP if self._cursor else now - INITIAL_LOOKBACK
            names = {cam.mac: cam.name for cam in self._camera_list()}
            fetched = self._wyze.events(since=since, until=now, limit=_WATCH_FETCH, oldest_first=True)
            self._remember(fetched)
            unseen = [event for event in fetched if event.id not in self._reported]
            batch = unseen[:max_events]

            reports = []
            for event in batch:
                detection, vision_error = None, None
                if event.image_url:
                    try:
                        data, mime = self._wyze.event_image(event)
                        detection = self._detector.detect(data, mime)
                    except (ImageError, VisionError) as err:
                        vision_error = str(err)
                reports.append(
                    self._report(names.get(event.mac, event.mac), event, event.time, detection, vision_error)
                )
                self._reported[event.id] = None
            while len(self._reported) > _REMEMBERED_EVENTS:
                self._reported.popitem(last=False)
            if batch:
                self._cursor = max(self._cursor or batch[-1].time, batch[-1].time)
            elif self._cursor is None:
                self._cursor = now - INITIAL_LOOKBACK

            return {
                "checked_from": _iso(since),
                "checked_to": _iso(now),
                "new_events": reports,
                "more_pending": len(unseen) > len(batch),
                "alert_counts": {
                    kind: sum(any(alert["type"] == kind for alert in report["alerts"]) for report in reports)
                    for kind in _ALERT_TYPES
                },
            }

    def set_armed(self, mode: ArmMode) -> dict[str, Any]:
        self._arm.set_mode(mode)
        return self._arm.describe(self._clock())

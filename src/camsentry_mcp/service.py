"""The camera-watching logic behind the MCP tools and the background watcher. Knows nothing about MCP."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .config import ALERT_TYPES
from .images import ImageError
from .notify import MAX_TEXT, Message, Notifier, NotifyError, compose
from .policy import MEMORY, ArmMode, ArmState, assess
from .store import Store
from .timeline import build_incidents, is_notable, search_reports
from .vision import VisionDetector, VisionError, VisionUnavailable, clean_text
from .wyze import Camera, CameraEvent, WyzeCloud

# The first time it runs, the watcher works through the last few hours so that the timeline and
# search have something in them straight away.
INITIAL_LOOKBACK = timedelta(hours=6)
# Wyze uploads events some time after they start, so each poll re-reads a little history.
_OVERLAP = timedelta(minutes=5)
_LATEST_IMAGE_WINDOW = timedelta(hours=24)
# News older than this is not worth telling: the chat model gets two hours (the timeline covers
# the rest), a phone gets half an hour.
_REPORT_WINDOW = timedelta(hours=2)
_NOTIFY_WINDOW = timedelta(minutes=30)
_CAMERA_CACHE_SECONDS = 60
_REMEMBERED_EVENTS = 2000
_POLL_FETCH = 100
MAX_RULES = 8
MAX_RULE_TEXT = 160
# A chat model may text the user on its own initiative only this often.
MANUAL_TEXTS_PER_HOUR = 6


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
        store: Store,
        notifier: Notifier,
        notify_types: frozenset[str],
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self._wyze = wyze
        self._detector = detector
        self._arm = arm
        self._store = store
        self._notifier = notifier
        self._notify_types = notify_types
        self._clock = clock
        self._state_lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._cameras: list[Camera] = []
        self._cameras_at: datetime | None = None
        self._events: OrderedDict[str, CameraEvent] = OrderedDict()

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

    def _names(self) -> dict[str, str]:
        return {cam.mac: cam.name for cam in self._camera_list()}

    def _remember(self, events: list[CameraEvent]) -> None:
        with self._state_lock:
            for event in events:
                self._events[event.id] = event
                self._events.move_to_end(event.id)
            while len(self._events) > _REMEMBERED_EVENTS:
                self._events.popitem(last=False)

    def _find_event(self, camera: Camera, event_id: str | None) -> CameraEvent:
        """A named event, or the camera's most recent still when no id is given."""
        now = self._clock()
        if event_id is None:
            recent = self._wyze.events(since=now - _LATEST_IMAGE_WINDOW, until=now, macs=[camera.mac])
            self._remember(recent)
            latest = next((candidate for candidate in recent if candidate.image_url), None)
            if latest is None:
                raise WatchError(f"Camera {camera.name!r} has recorded no still image in the last 24 hours.")
            return latest
        with self._state_lock:
            event = self._events.get(event_id)
        if event is None:
            recent = self._wyze.events(since=now - _LATEST_IMAGE_WINDOW, until=now, macs=[camera.mac], limit=100)
            self._remember(recent)
            event = next((candidate for candidate in recent if candidate.id == event_id), None)
        if event is None or event.mac != camera.mac:
            raise WatchError(
                f"No event {event_id!r} on camera {camera.name!r} in the last 24 hours. "
                "Use get_events to list valid event ids."
            )
        return event

    # -- analysis ----------------------------------------------------------

    def _analyse(self, event: CameraEvent, camera_name: str) -> dict[str, Any]:
        """Look at one still, compare it with the camera's previous one, and remember the result."""
        rules = [rule for rule in self._store.rules() if rule.camera_id in (None, event.mac)]
        detection, vision_error, matched = None, None, []
        if event.image_url:
            try:
                data, mime = self._wyze.event_image(event)
                detection = self._detector.detect(data, mime)
                verdicts = self._detector.check(data, mime, [rule.text for rule in rules])
                matched = [rule for rule, true in zip(rules, verdicts) if true]
            except VisionUnavailable:
                raise  # nothing is recorded, so the still is analysed once the model is back
            except (ImageError, VisionError) as err:
                vision_error = str(err)
        armed = self._arm.is_armed(event.time)
        previous = self._store.previous_report(event.mac, event.time, MEMORY)
        result = assess(detection, event.wyze_tags, armed, event.time, previous, matched)
        report: dict[str, Any] = {
            "event_id": event.id,
            "camera": camera_name,
            "camera_id": event.mac,
            "time": _iso(event.time),
            "kind": event.kind,
            "wyze_tags": list(event.wyze_tags),
            "armed": armed,
            "detection": detection.to_dict() if detection else None,
            "alerts": result.alerts,
            "new_alerts": result.new_alerts,
            "changes": result.changes,
            "person_since": _iso(result.person_since) if result.person_since else None,
        }
        if vision_error:
            report["vision_error"] = vision_error
        self._store.save_report(event.id, event.mac, event.time, report)
        return report

    def poll(self, max_events: int) -> int:
        """Analyse stills Wyze has recorded since the last poll, oldest first.

        Returns how many new stills are still waiting. Safe to call from several threads and
        processes: a still that is already in the store is never analysed again.
        """
        with self._poll_lock:
            now = self._clock()
            saved = self._store.get_setting("cursor")
            cursor = datetime.fromisoformat(saved) if saved else None
            since = cursor - _OVERLAP if cursor else now - INITIAL_LOOKBACK
            names = self._names()
            fetched = self._wyze.events(since=since, until=now, limit=_POLL_FETCH, oldest_first=True)
            self._remember(fetched)
            known = self._store.known_event_ids([event.id for event in fetched])
            fresh = [event for event in fetched if event.id not in known]
            batch = fresh[:max_events]
            for event in batch:
                self._analyse(event, names.get(event.mac, event.mac))
                reached = max(cursor, event.time) if cursor else event.time
                if reached != cursor:
                    cursor = reached
                    self._store.set_setting("cursor", _iso(cursor))
            if cursor is None:
                self._store.set_setting("cursor", _iso(now - INITIAL_LOOKBACK))
            self._store.prune(now)
            return len(fresh) - len(batch)

    # -- tools -------------------------------------------------------------

    def _texted_alerts(self, camera_id: str) -> frozenset[str]:
        """The alert types that reach the user's phone for one camera: its own choice, else the default."""
        chosen = json.loads(self._store.get_setting(f"notify:{camera_id}") or "null")
        return frozenset(chosen) if chosen is not None else self._notify_types

    def list_cameras(self) -> dict[str, Any]:
        return {
            "cameras": [
                {
                    "name": cam.name,
                    "id": cam.mac,
                    "model": cam.model,
                    "online": cam.online,
                    "alerts_sent_to_phone": [kind for kind in ALERT_TYPES if kind in self._texted_alerts(cam.mac)],
                }
                for cam in self._camera_list()
            ],
            "arming": self._arm.describe(self._clock()),
            "notifications": self._notifier.channels(),
        }

    def set_camera_alerts(self, camera: str, alert_types: list[str] | None) -> dict[str, Any]:
        """Choose what one camera may text about. None restores the default; an empty list silences it."""
        cam = self._resolve(camera)
        unknown = sorted(set(alert_types or []) - set(ALERT_TYPES))
        if unknown:
            raise WatchError(f"Unknown alert types {unknown}. Choose from: {', '.join(ALERT_TYPES)}.")
        self._store.set_setting(f"notify:{cam.mac}", json.dumps(sorted(set(alert_types)) if alert_types is not None else None))
        sent = [kind for kind in ALERT_TYPES if kind in self._texted_alerts(cam.mac)]
        self._announce_change(f"{cam.name} now texts you about: {', '.join(sent) or 'nothing'}.")
        return {"camera": cam.name, "alerts_sent_to_phone": sent}

    def get_events(self, camera: str | None, minutes: int, limit: int) -> dict[str, Any]:
        macs = [self._resolve(camera).mac] if camera else []
        names = self._names()
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
        event = self._find_event(cam, event_id)
        data, mime = self._wyze.event_image(event)
        return {"camera": cam.name, "event_id": event.id, "time": _iso(event.time)}, data, mime

    def analyze(self, camera: str, event_id: str | None) -> dict[str, Any]:
        cam = self._resolve(camera)
        event = self._find_event(cam, event_id)
        if event.image_url is None:
            raise WatchError("That event has no still image to analyse.")
        report = self._analyse(event, cam.name)
        if "vision_error" in report:
            raise WatchError(report["vision_error"])
        return report

    def watch(self, max_events: int) -> dict[str, Any]:
        backlog = self.poll(max_events)
        now = self._clock()
        reports, more = self._store.take_unreported(now, max_events, now - _REPORT_WINDOW)
        return {
            "new_events": reports,
            "more_pending": more or backlog > 0,
            "alert_counts": {
                kind: sum(any(alert["type"] == kind for alert in report["alerts"]) for report in reports)
                for kind in ALERT_TYPES
            },
        }

    def timeline(self, camera: str | None, hours: int, include_quiet: bool) -> dict[str, Any]:
        camera_id = self._resolve(camera).mac if camera else None
        reports = self._store.reports_since(self._clock() - timedelta(hours=hours), camera_id)
        incidents = build_incidents(reports)
        shown = incidents if include_quiet else [incident for incident in incidents if is_notable(incident)]
        return {"incidents": shown[:50], "quiet_incidents_hidden": len(incidents) - len(shown), "stills_analysed": len(reports)}

    def search(self, query: str, hours: int, limit: int) -> dict[str, Any]:
        reports = self._store.reports_since(self._clock() - timedelta(hours=hours))
        return {"matches": search_reports(reports, query, limit), "stills_searched": len(reports)}

    def set_armed(self, mode: ArmMode) -> dict[str, Any]:
        before = self._arm.mode()
        self._arm.set_mode(mode)
        if mode != before:
            self._announce_change(f"Arming mode is now '{mode}' (was '{before}').")
        return self._arm.describe(self._clock())

    # -- custom checks -----------------------------------------------------

    def list_rules(self) -> dict[str, Any]:
        names = self._names()
        return {
            "rules": [
                {"id": rule.id, "rule": rule.text, "camera": names.get(rule.camera_id, rule.camera_id) if rule.camera_id else "all cameras"}
                for rule in self._store.rules()
            ]
        }

    def add_rule(self, rule: str, camera: str | None) -> dict[str, Any]:
        text = clean_text(rule, MAX_RULE_TEXT)
        if len(text) < 4:
            raise WatchError("Describe what to watch for in a short sentence, for example 'the garage door is open'.")
        if len(self._store.rules()) >= MAX_RULES:
            raise WatchError(f"There are already {MAX_RULES} watch rules. Remove one before adding another.")
        cam = self._resolve(camera) if camera else None
        added = self._store.add_rule(text, cam.mac if cam else None, self._clock())
        return {"id": added.id, "rule": added.text, "camera": cam.name if cam else "all cameras"}

    def remove_rule(self, rule_id: int) -> dict[str, Any]:
        text = next((rule.text for rule in self._store.rules() if rule.id == rule_id), None)
        if text is None or not self._store.remove_rule(rule_id):
            raise WatchError(f"There is no watch rule with id {rule_id}. Use list_watch_rules to see them.")
        self._announce_change(f"Watch rule removed: {text}.")
        return {"removed": rule_id}

    # -- telling the user --------------------------------------------------

    def _announce_change(self, what: str) -> None:
        """Tell the user's phone when a chat model changes what they are alerted about.

        A model can be talked into things, including by text a camera sees. It may quieten alerts,
        but never without the user hearing of it.
        """
        if self._notifier.channels():
            try:
                self._notifier.send(Message("Alert settings changed", what))
            except NotifyError:
                pass  # the change itself stands; the watcher reports delivery problems

    def notify_pending(self) -> int:
        """Send a notification for each freshly analysed still that deserves one. Returns how many were sent."""
        if not self._notifier.channels():
            return 0
        now = self._clock()
        reports = self._store.take_unnotified(now, 20, now - _NOTIFY_WINDOW)
        sent = 0
        for index, report in enumerate(reports):
            message = compose(report, self._texted_alerts(report["camera_id"]))
            if message is None:
                continue
            try:
                self._notifier.send(message)
            except NotifyError:
                for unsent in reports[index:]:
                    self._store.release_notification(unsent["event_id"])
                raise
            sent += 1
        return sent

    def notify_user(self, message: str) -> dict[str, Any]:
        """Send a message the chat model wrote. The destination is fixed by the user; only the text is the model's."""
        text = clean_text(message, MAX_TEXT)
        if not text:
            raise WatchError("The message is empty.")
        now = self._clock()
        recent = [stamp for stamp in json.loads(self._store.get_setting("manual_texts") or "[]") if now - datetime.fromisoformat(stamp) < timedelta(hours=1)]
        if len(recent) >= MANUAL_TEXTS_PER_HOUR:
            raise WatchError(f"Limit reached: at most {MANUAL_TEXTS_PER_HOUR} messages per hour can be sent this way.")
        try:
            delivery = self._notifier.send(Message("Camera assistant", text))
        except NotifyError as err:
            raise WatchError(str(err)) from None
        self._store.set_setting("manual_texts", json.dumps([*recent, _iso(now)]))
        return {"sent_via": delivery.delivered, "remaining_this_hour": MANUAL_TEXTS_PER_HOUR - len(recent) - 1}

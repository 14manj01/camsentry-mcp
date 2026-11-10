"""Turns raw detections into person / pet / package / intruder alerts."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import datetime
from typing import Any, Literal

from .config import ArmedHours
from .vision import Detection

ArmMode = Literal["on", "off", "schedule"]

_PERSON_TAGS = {"Person", "Face"}
_PET_TAGS = {"Pet", "Dog", "Cat"}
_PACKAGE_TAGS = {"Package"}


class ArmState:
    """Whether a person on camera should be treated as an intruder.

    "schedule" follows CAMSENTRY_ARMED_HOURS (never armed when unset); "on" and "off" override it
    until changed or the server restarts.
    """

    def __init__(self, hours: ArmedHours | None) -> None:
        self._hours = hours
        self._mode: ArmMode = "schedule"
        self._lock = threading.Lock()

    def set_mode(self, mode: ArmMode) -> None:
        with self._lock:
            self._mode = mode

    def is_armed(self, when: datetime) -> bool:
        with self._lock:
            mode = self._mode
        if mode != "schedule":
            return mode == "on"
        return self._hours is not None and self._hours.contains(when.astimezone().time())

    def describe(self, now: datetime) -> dict[str, Any]:
        with self._lock:
            mode = self._mode
        return {
            "mode": mode,
            "armed_now": self.is_armed(now),
            "armed_hours": str(self._hours) if self._hours else None,
        }


def build_alerts(detection: Detection | None, wyze_tags: Iterable[str], armed: bool) -> list[dict[str, str]]:
    """Combine the local model's detection with Wyze's own AI tags (either source can raise an alert)."""
    tags = set(wyze_tags)
    alerts: list[dict[str, str]] = []

    def add(kind: str, seen_locally: str | None, tagged: bool) -> bool:
        reasons = [reason for reason in (seen_locally, "tagged by Wyze" if tagged else None) if reason]
        if reasons:
            alerts.append({"type": kind, "reason": "; ".join(reasons)})
        return bool(reasons)

    people = detection.people if detection else 0
    pets = detection.pets if detection else ()
    packages = detection.packages if detection else 0

    person = add("person", f"{people} visible" if people else None, bool(tags & _PERSON_TAGS))
    add("pet", ", ".join(pets) if pets else None, bool(tags & _PET_TAGS))
    add("package", f"{packages} visible" if packages else None, bool(tags & _PACKAGE_TAGS))

    if person and armed:
        alerts.append({"type": "intruder", "reason": "person detected while armed"})
    elif detection is not None and detection.suspicious and people:
        reason = detection.suspicious_reason or "suspicious behaviour"
        alerts.append({"type": "intruder", "reason": f"local model flagged: {reason}"})
    return alerts

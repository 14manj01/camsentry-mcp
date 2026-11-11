"""Turns what one still shows, plus what the previous still showed, into alerts.

A single frame says "a person is here". Comparing it with the frame before says "a person has
been here for six minutes" or "the package that was here is gone", which is what is worth a text.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from .config import ALERT_TYPES, QUIET_ALERTS, ArmedHours
from .store import Rule, Store
from .vision import Detection

ArmMode = Literal["on", "off", "schedule"]

# Stills from one camera closer together than this belong to the same visit.
INCIDENT_GAP = timedelta(minutes=10)
# How far back a still is still compared with the one before it (is the package still there?).
MEMORY = timedelta(hours=24)
LINGER_AFTER = timedelta(minutes=3)

_PERSON_TAGS = {"Person", "Face"}
_PET_TAGS = {"Pet", "Dog", "Cat"}
_VEHICLE_TAGS = {"Vehicle", "Car", "School Bus", "Delivery Truck"}
_PACKAGE_TAGS = {"Package"}

_URGENT = {"intruder", "package_taken"}
# Alerts about something that stays put are compared across the whole memory window, so a parcel
# on the porch or a car in the drive is announced once, not at every still.
_LASTING = {"package", "vehicle", "rule"}


class ArmState:
    """Whether a person on camera should be treated as an intruder.

    "schedule" follows CAMSENTRY_ARMED_HOURS (never armed when unset); "on" and "off" override it.
    The mode is kept in the store so that it survives restarts and is shared with the watcher.
    """

    def __init__(self, hours: ArmedHours | None, store: Store) -> None:
        self._hours = hours
        self._store = store

    def mode(self) -> ArmMode:
        stored = self._store.get_setting("arm_mode")
        return stored if stored in ("on", "off") else "schedule"  # type: ignore[return-value]

    def set_mode(self, mode: ArmMode) -> None:
        self._store.set_setting("arm_mode", mode)

    def is_armed(self, when: datetime) -> bool:
        mode = self.mode()
        if mode != "schedule":
            return mode == "on"
        return self._hours is not None and self._hours.contains(when.astimezone().time())

    def describe(self, now: datetime) -> dict[str, Any]:
        return {
            "mode": self.mode(),
            "armed_now": self.is_armed(now),
            "armed_hours": str(self._hours) if self._hours else None,
        }


@dataclass(frozen=True)
class Assessment:
    alerts: list[dict[str, Any]]
    new_alerts: list[str]  # keys of the alerts that were not already true in the previous still
    changes: list[str]
    person_since: datetime | None


def alert_key(alert: dict[str, Any]) -> str:
    return f"rule:{alert['rule_id']}" if alert["type"] == "rule" else alert["type"]


def _priority(kind: str) -> str:
    return "urgent" if kind in _URGENT else "low" if kind in QUIET_ALERTS else "normal"


def _minutes(span: timedelta) -> int:
    return max(1, round(span.total_seconds() / 60))


def assess(
    detection: Detection | None,
    wyze_tags: Iterable[str],
    armed: bool,
    when: datetime,
    previous: dict[str, Any] | None,
    matched_rules: Sequence[Rule] = (),
) -> Assessment:
    """Decide the alerts for one still.

    `previous` is the stored report of the same camera's preceding still (within MEMORY), or None.
    Either the local model or Wyze's own tags can raise person, pet, vehicle and package alerts.
    """
    tags = set(wyze_tags)
    alerts: list[dict[str, Any]] = []

    def add(kind: str, reason: str, **extra: Any) -> None:
        alerts.append({"type": kind, "reason": reason, "priority": _priority(kind), **extra})

    def seen(kind: str, locally: str | None, tagged: bool) -> bool:
        reasons = [reason for reason in (locally, "tagged by Wyze" if tagged else None) if reason]
        if reasons:
            add(kind, "; ".join(reasons))
        return bool(reasons)

    people = detection.people if detection else 0
    pets = detection.pets if detection else ()
    vehicles = detection.vehicles if detection else 0
    packages = detection.packages if detection else 0

    before = previous or {}
    before_detection = before.get("detection") or {}
    before_keys = {alert_key(alert) for alert in before.get("alerts", [])}
    same_visit = previous is not None and when - datetime.fromisoformat(before["time"]) <= INCIDENT_GAP
    visit_keys = before_keys if same_visit else set()
    person_before = same_visit and "person" in before_keys

    person = seen("person", f"{people} visible" if people else None, bool(tags & _PERSON_TAGS))
    seen("pet", ", ".join(pets) if pets else None, bool(tags & _PET_TAGS))
    seen("vehicle", f"{vehicles} visible" if vehicles else None, bool(tags & _VEHICLE_TAGS))
    seen("package", f"{packages} visible" if packages else None, bool(tags & _PACKAGE_TAGS))

    person_since = None
    if person:
        earlier = before.get("person_since") if person_before else None
        person_since = datetime.fromisoformat(earlier) if earlier else when
        if when - person_since >= LINGER_AFTER:
            add("lingering", f"a person has been in view for about {_minutes(when - person_since)} minutes")

    package_gone = detection is not None and before_detection.get("packages", 0) > 0 and packages == 0
    if package_gone and (person or "person" in before_keys):
        add("package_taken", "a package that was in view is gone after a person was there")

    if person and armed:
        add("intruder", "person detected while armed")
    elif detection is not None and detection.suspicious and people:
        add("intruder", f"local model flagged: {detection.suspicious_reason or 'suspicious behaviour'}")

    for rule in matched_rules:
        add("rule", rule.text, rule_id=rule.id)

    changes: list[str] = []
    if person and not person_before:
        changes.append("person arrived")
    if detection is not None:
        if person_before and not person:
            changes.append("person left")
        if previous is not None and before.get("detection") is not None:
            had_vehicle = before_detection.get("vehicles", 0) > 0
            if vehicles and not had_vehicle:
                changes.append("vehicle arrived")
            if had_vehicle and not vehicles:
                changes.append("vehicle left")
            if packages and not before_detection.get("packages", 0):
                changes.append("package appeared")
            if package_gone:
                changes.append("package gone")

    new_alerts = []
    for alert in alerts:
        key = alert_key(alert)
        already = before_keys if alert["type"] in _LASTING else visit_keys
        if alert["type"] == "package_taken" or key not in already:
            new_alerts.append(key)

    alerts.sort(key=lambda alert: ALERT_TYPES.index(alert["type"]))
    return Assessment(alerts, new_alerts, changes, person_since)

"""Pieces individual stills together: visits ("incidents") and a keyword search over what was seen."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .config import ALERT_TYPES, QUIET_ALERTS
from .policy import INCIDENT_GAP

_STORY_HEAD, _STORY_TAIL = 4, 3
_STOP_WORDS = frozenset(
    "a an and any are at did do does for from has have i in is it me my of on or show that the there to was were what when who with".split()
)


def _clock(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")


def _story_line(report: dict[str, Any]) -> str:
    detection = report.get("detection") or {}
    text = detection.get("summary") or report.get("vision_error") or "no image"
    changes = report.get("changes") or []
    return f"{_clock(report['time'])} {text}" + (f" [{'; '.join(changes)}]" if changes else "")


def _incident(reports: list[dict[str, Any]]) -> dict[str, Any]:
    detections = [report["detection"] for report in reports if report.get("detection")]
    kinds = {alert["type"] for report in reports for alert in report["alerts"]}
    lines = [_story_line(report) for report in reports]
    if len(lines) > _STORY_HEAD + _STORY_TAIL + 1:
        skipped = len(lines) - _STORY_HEAD - _STORY_TAIL
        lines = [*lines[:_STORY_HEAD], f"... {skipped} more stills ...", *lines[-_STORY_TAIL:]]
    return {
        "camera": reports[0]["camera"],
        "start": reports[0]["time"],
        "end": reports[-1]["time"],
        "stills": len(reports),
        "people": max((d["people"] for d in detections), default=0),
        "pets": sorted({pet for d in detections for pet in d["pets"]}),
        "vehicles": max((d["vehicles"] for d in detections), default=0),
        "packages": max((d["packages"] for d in detections), default=0),
        "alerts": [kind for kind in ALERT_TYPES if kind in kinds],
        "changes": [change for report in reports for change in report.get("changes") or []],
        "story": lines,
        "event_ids": [report["event_id"] for report in reports][-10:],
    }


def build_incidents(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group reports (oldest first) into visits per camera, newest visit first."""
    groups: dict[str, list[list[dict[str, Any]]]] = {}
    for report in reports:
        visits = groups.setdefault(report["camera_id"], [])
        when = datetime.fromisoformat(report["time"])
        if visits and when - datetime.fromisoformat(visits[-1][-1]["time"]) <= INCIDENT_GAP:
            visits[-1].append(report)
        else:
            visits.append([report])
    incidents = [_incident(visit) for visits in groups.values() for visit in visits]
    return sorted(incidents, key=lambda incident: datetime.fromisoformat(incident["start"]), reverse=True)


def is_notable(incident: dict[str, Any]) -> bool:
    """Something happened: an alert worth attention, or a change. A parked car alone is not notable."""
    return bool(set(incident["alerts"]) - QUIET_ALERTS or incident["changes"])


def _words(text: str) -> set[str]:
    """Lowercase words with a trailing plural "s" removed, so "dogs" finds "dog"."""
    return {word[:-1] if len(word) > 3 and word.endswith("s") else word for word in re.findall(r"[a-z0-9]+", text.lower())}


def _searchable(report: dict[str, Any]) -> set[str]:
    detection = report.get("detection") or {}
    parts = [
        report["camera"],
        detection.get("summary", ""),
        " ".join(detection.get("pets", [])),
        " ".join(report.get("wyze_tags", [])),
        " ".join(report.get("changes") or []),
    ]
    for alert in report["alerts"]:
        parts += [alert["type"].replace("_", " "), alert["reason"]]
    return _words(" ".join(parts))


def search_reports(reports: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    """Reports that mention the query's words, best match first, then newest first.

    Stills matching every word win; if none does, stills matching some of the words are returned.
    """
    terms = _words(query) - _STOP_WORDS
    if not terms:
        return []
    scored = [(len(terms & _searchable(report)), report) for report in reports]
    best = max((score for score, _ in scored), default=0)
    if best == 0:
        return []
    floor = len(terms) if best == len(terms) else 1
    hits = [(score, report) for score, report in scored if score >= floor]
    hits.sort(key=lambda hit: (hit[0], hit[1]["time"]), reverse=True)
    return [
        {
            "event_id": report["event_id"],
            "camera": report["camera"],
            "time": report["time"],
            "summary": (report.get("detection") or {}).get("summary", ""),
            "alerts": [alert["type"] for alert in report["alerts"]],
            "matched_words": score,
        }
        for score, report in hits[:limit]
    ]

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import FakeBridge, FakeDetector, FakeWyze, at, event
from camsentry_mcp.config import ArmedHours
from camsentry_mcp.policy import ArmState
from camsentry_mcp.server import build_server
from camsentry_mcp.service import CameraWatch, WatchError
from camsentry_mcp.vision import Detection
from camsentry_mcp.wyze import Camera, NotLoggedIn

CAMERAS = [Camera("MAC1", "Front Door", "HL_CAM4", True), Camera("MAC2", "Backyard", "WYZE_CAKP2JFUS", True)]
PERSON = Detection(1, (), 0, False, "", "A person stands at the door.")


class Clock:
    def __init__(self, start: str) -> None:
        self.now = at(start)

    def __call__(self) -> datetime:
        return self.now

    def set(self, text: str) -> None:
        self.now = at(text)


def make(events=(), *, detector=None, hours: str | None = None, live=None, now: str = "2026-03-01 12:00"):
    wyze = FakeWyze(CAMERAS, list(events))
    clock = Clock(now)
    detector = detector or FakeDetector()
    arm = ArmState(ArmedHours.parse(hours) if hours else None)
    return CameraWatch(wyze, detector, arm, live=live, clock=clock), wyze, clock, detector


def ids(result: dict) -> list[str]:
    return [report["event_id"] for report in result["new_events"]]


def test_watch_reports_each_event_once() -> None:
    watch, wyze, clock, _ = make([event("e1", "2026-03-01 11:50"), event("e2", "2026-03-01 11:55")])
    assert ids(watch.watch(5)) == ["e1", "e2"]

    clock.set("2026-03-01 12:01")
    assert ids(watch.watch(5)) == []

    wyze.all_events.append(event("e3", "2026-03-01 12:02"))
    clock.set("2026-03-01 12:03")
    assert ids(watch.watch(5)) == ["e3"]


def test_first_watch_ignores_events_older_than_the_initial_lookback() -> None:
    watch, *_ = make([event("old", "2026-03-01 11:30"), event("recent", "2026-03-01 11:50")])
    assert ids(watch.watch(5)) == ["recent"]


def test_watch_picks_up_events_that_wyze_uploads_late() -> None:
    watch, wyze, clock, _ = make([event("e1", "2026-03-01 11:58")])
    assert ids(watch.watch(5)) == ["e1"]

    # Started before e1 but only reached the Wyze cloud afterwards.
    wyze.all_events.append(event("late", "2026-03-01 11:56", mac="MAC2"))
    clock.set("2026-03-01 12:01")
    assert ids(watch.watch(5)) == ["late"]


def test_watch_batches_a_backlog_oldest_first() -> None:
    backlog = [event(f"e{i}", f"2026-03-01 11:5{i}") for i in range(5)]
    watch, _, _, detector = make(backlog)

    first = watch.watch(2)
    assert (ids(first), first["more_pending"]) == (["e0", "e1"], True)
    assert detector.calls == 2
    second = watch.watch(2)
    assert (ids(second), second["more_pending"]) == (["e2", "e3"], True)
    third = watch.watch(2)
    assert (ids(third), third["more_pending"]) == (["e4"], False)


def test_watch_survives_a_failing_vision_model_and_falls_back_to_wyze_tags() -> None:
    events = [event("e1", "2026-03-01 11:55", tags=("Person",))]
    watch, *_ = make(events, detector=FakeDetector(error="Could not reach the local vision model server."), hours="00:00-23:59")

    (report,) = watch.watch(5)["new_events"]

    assert report["detection"] is None
    assert "Could not reach" in report["vision_error"]
    assert [alert["type"] for alert in report["alerts"]] == ["person", "intruder"]


def test_watch_counts_alerts_and_names_the_camera() -> None:
    events = [event("e1", "2026-03-01 11:55", mac="MAC2"), event("sound", "2026-03-01 11:56", image=False)]
    watch, _, _, detector = make(events, detector=FakeDetector(PERSON))

    result = watch.watch(5)

    assert [report["camera"] for report in result["new_events"]] == ["Backyard", "Front Door"]
    assert result["alert_counts"] == {"person": 1, "pet": 0, "package": 0, "intruder": 0}
    assert detector.calls == 1, "events without an image are not sent to the vision model"


def test_arming_is_judged_at_the_time_of_the_event_not_the_time_of_the_question() -> None:
    watch, *_ = make([event("night", "2026-03-01 02:00")], detector=FakeDetector(PERSON), hours="22:00-06:00", now="2026-03-01 09:00")

    report = watch.analyze("Front Door", "night")

    assert report["armed"] is True
    assert "intruder" in [alert["type"] for alert in report["alerts"]]


def test_set_armed_on_makes_a_daytime_person_an_intruder() -> None:
    watch, *_ = make([event("e1", "2026-03-01 11:55")], detector=FakeDetector(PERSON))
    assert "intruder" not in [a["type"] for a in watch.analyze("Front Door", "e1")["alerts"]]

    assert watch.set_armed("on")["armed_now"] is True
    assert "intruder" in [a["type"] for a in watch.analyze("Front Door", "e1")["alerts"]]


def test_without_a_bridge_the_latest_event_image_is_analysed() -> None:
    events = [event("older", "2026-03-01 10:00"), event("newest-no-image", "2026-03-01 11:59", image=False), event("newest-image", "2026-03-01 11:30")]
    watch, *_ = make(events)

    report = watch.analyze("front door", None)

    assert (report["source"], report["event_id"]) == ("event", "newest-image")


def test_with_a_bridge_a_live_frame_is_analysed() -> None:
    watch, *_ = make([event("e1", "2026-03-01 11:55")], live=FakeBridge())

    report = watch.analyze("Front Door", None)

    assert (report["source"], report["event_id"], report["time"]) == ("live", None, at("2026-03-01 12:00").isoformat(timespec="seconds"))


def test_no_recent_image_and_no_bridge_is_explained() -> None:
    watch, *_ = make([])
    with pytest.raises(WatchError, match="CAMSENTRY_BRIDGE_URL"):
        watch.view("Front Door", None)


def test_unknown_camera_lists_the_available_ones() -> None:
    watch, *_ = make([])
    with pytest.raises(WatchError, match="Backyard, Front Door"):
        watch.analyze("Garage", None)


def test_cameras_are_addressable_by_id() -> None:
    watch, *_ = make([event("e1", "2026-03-01 11:55", mac="MAC2")])
    assert watch.analyze("mac2", None)["camera"] == "Backyard"


def test_an_event_cannot_be_read_through_a_different_camera() -> None:
    watch, *_ = make([event("e1", "2026-03-01 11:55", mac="MAC2")])
    watch.get_events(None, 60, 20)
    with pytest.raises(WatchError, match="No event 'e1'"):
        watch.view("Front Door", "e1")


def test_get_events_filters_by_camera_and_window() -> None:
    events = [event("a", "2026-03-01 11:55"), event("b", "2026-03-01 11:50", mac="MAC2", tags=("Pet",)), event("c", "2026-03-01 09:00", mac="MAC2")]
    watch, *_ = make(events)

    listed = watch.get_events("Backyard", 60, 20)["events"]

    assert [(e["event_id"], e["camera"], e["wyze_tags"]) for e in listed] == [("b", "Backyard", ["Pet"])]


# -- through the MCP tool layer ------------------------------------------------


def call(watch: CameraWatch, tool: str, arguments: dict):
    return asyncio.run(build_server(watch).call_tool(tool, arguments))


def test_tool_returns_the_frame_as_image_content() -> None:
    watch, *_ = make([event("e1", "2026-03-01 11:55")])

    result = call(watch, "view_camera", {"camera": "Front Door"})

    meta, image = result.content
    assert json.loads(meta.text)["event_id"] == "e1"
    assert (image.type, image.mime_type) == ("image", "image/jpeg")


def test_tool_shows_the_login_hint_to_the_model_when_signed_out() -> None:
    watch, wyze, *_ = make([])

    def signed_out() -> list[Camera]:
        raise NotLoggedIn("Not connected to Wyze. Run `camsentry-mcp login` in a terminal.")

    wyze.cameras = signed_out
    with pytest.raises(ToolError, match="camsentry-mcp login"):
        call(watch, "list_cameras", {})


def test_tool_rejects_out_of_range_arguments() -> None:
    watch, *_ = make([])
    with pytest.raises(ToolError):
        call(watch, "watch_cameras", {"max_events": 500})

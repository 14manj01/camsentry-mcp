from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import event, seen
from camsentry_mcp.server import build_server
from camsentry_mcp.service import MANUAL_TEXTS_PER_HOUR, MAX_RULES, CameraWatch, WatchError
from camsentry_mcp.vision import VisionError, VisionUnavailable
from camsentry_mcp.watcher import Watcher
from camsentry_mcp.wyze import NotLoggedIn

PERSON = seen(people=1, summary="A person stands at the door.")
PHONE = "+15550100123"


def ids(result: dict) -> list[str]:
    return [report["event_id"] for report in result["new_events"]]


def kinds(report: dict) -> list[str]:
    return [alert["type"] for alert in report["alerts"]]


# -- watching ------------------------------------------------------------------


def test_watch_reports_each_still_once_even_across_a_restart(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:50"), event("e2", "2026-03-01 11:55")])
    assert ids(r.watch.watch(5)) == ["e1", "e2"]

    r.clock.set("2026-03-01 12:01")
    assert ids(r.restart().watch(5)) == []

    r.wyze.all_events.append(event("e3", "2026-03-01 12:02"))
    r.clock.set("2026-03-01 12:03")
    assert ids(r.restart().watch(5)) == ["e3"]
    assert r.detector.calls == 3, "no still is sent to the vision model twice"


def test_first_run_remembers_the_last_few_hours_but_only_reports_what_is_recent(rig) -> None:
    r = rig([event("yesterday", "2026-02-28 12:00"), event("this-morning", "2026-03-01 08:00"), event("recent", "2026-03-01 11:50")])
    assert ids(r.watch.watch(5)) == ["recent"]
    assert r.watch.timeline(None, 48, include_quiet=True)["stills_analysed"] == 2, "yesterday is before the first-run lookback"
    assert ids(r.watch.watch(5)) == [], "the morning still is history, not news"


def test_watch_picks_up_stills_that_wyze_uploads_late(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:58")])
    assert ids(r.watch.watch(5)) == ["e1"]

    # Started before e1 but only reached the Wyze cloud afterwards.
    r.wyze.all_events.append(event("late", "2026-03-01 11:56", mac="MAC2"))
    r.clock.set("2026-03-01 12:01")
    assert ids(r.watch.watch(5)) == ["late"]


def test_watch_works_through_a_backlog_in_batches(rig) -> None:
    r = rig([event(f"e{i}", f"2026-03-01 11:5{i}") for i in range(5)])
    first = r.watch.watch(2)
    assert (ids(first), first["more_pending"]) == (["e0", "e1"], True)
    second = r.watch.watch(2)
    assert (ids(second), second["more_pending"]) == (["e2", "e3"], True)
    third = r.watch.watch(2)
    assert (ids(third), third["more_pending"]) == (["e4"], False)


def test_stills_analysed_in_the_background_are_still_reported_to_the_model(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55")])
    r.watch.poll(10)
    assert ids(r.watch.watch(5)) == ["e1"]
    assert r.detector.calls == 1


def test_when_the_vision_model_is_down_stills_wait_and_are_analysed_later(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55")])
    r.detector.error = VisionUnavailable("Could not reach the local vision model server.")
    with pytest.raises(VisionUnavailable):
        r.watch.watch(5)

    r.detector.error = None
    r.detector.result = PERSON
    r.clock.set("2026-03-01 12:05")
    (report,) = r.watch.watch(5)["new_events"]
    assert kinds(report) == ["person"]


def test_a_still_that_cannot_be_read_is_recorded_once_and_does_not_block_the_rest(rig) -> None:
    r = rig([event("bad", "2026-03-01 11:50", tags=("Person",)), event("good", "2026-03-01 11:55")])
    r.wyze.broken_images.add("bad")

    bad, good = r.watch.watch(5)["new_events"]

    assert bad["detection"] is None and "403" in bad["vision_error"]
    assert kinds(bad) == ["person"], "Wyze's own tag still raises the alert"
    assert good["detection"] is not None
    assert ids(r.watch.watch(5)) == []


def test_an_unusable_model_answer_for_one_still_does_not_block_the_rest(rig) -> None:
    r = rig([event("garbled", "2026-03-01 11:50"), event("fine", "2026-03-01 11:55")])
    answers = iter([VisionError("The vision model answered with malformed JSON."), None])

    def detect(image, mime):
        problem = next(answers)
        if problem:
            raise problem
        return PERSON

    r.detector.detect = detect
    garbled, fine = r.watch.watch(5)["new_events"]
    assert "malformed" in garbled["vision_error"]
    assert kinds(fine) == ["person"]


def test_stills_are_pieced_together_across_polls(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50"), event("b", "2026-03-01 11:54"), event("c", "2026-03-01 11:58")])
    r.detector.script = [PERSON, PERSON, seen(summary="An empty step.")]

    a, b, c = r.watch.watch(5)["new_events"]

    assert (a["changes"], a["new_alerts"]) == (["person arrived"], ["person"])
    assert (kinds(b), b["new_alerts"]) == (["lingering", "person"], ["lingering"])
    assert (kinds(c), c["changes"]) == ([], ["person left"])


def test_arming_is_judged_at_the_time_of_the_still(rig) -> None:
    r = rig([event("night", "2026-03-01 02:00")], hours="22:00-06:00", now="2026-03-01 09:00")
    r.detector.result = PERSON
    report = r.watch.analyze("Front Door", "night")
    assert report["armed"] is True and kinds(report) == ["intruder", "person"]


def test_set_armed_is_remembered_after_a_restart(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55")])
    r.detector.result = PERSON
    assert r.watch.set_armed("on")["armed_now"] is True
    assert "intruder" in kinds(r.restart().analyze("Front Door", "e1"))


# -- looking at one camera -----------------------------------------------------


def test_latest_still_with_an_image_is_analysed_when_no_event_is_named(rig) -> None:
    r = rig([event("older", "2026-03-01 10:00"), event("newest-no-image", "2026-03-01 11:59", image=False), event("newest-image", "2026-03-01 11:30")])
    assert r.watch.analyze("front door", None)["event_id"] == "newest-image"


def test_camera_without_recent_stills_is_explained(rig) -> None:
    with pytest.raises(WatchError, match="no still image in the last 24 hours"):
        rig().watch.view("Backyard", None)


def test_unknown_camera_lists_the_available_ones(rig) -> None:
    with pytest.raises(WatchError, match="Backyard, Front Door"):
        rig().watch.analyze("Garage", None)


def test_cameras_are_addressable_by_id(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55", mac="MAC2")])
    assert r.watch.analyze("mac2", None)["camera"] == "Backyard"


def test_an_event_cannot_be_read_through_a_different_camera(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55", mac="MAC2")])
    r.watch.get_events(None, 60, 20)
    with pytest.raises(WatchError, match="No event 'e1'"):
        r.watch.view("Front Door", "e1")


def test_get_events_filters_by_camera_and_window(rig) -> None:
    r = rig([event("a", "2026-03-01 11:55"), event("b", "2026-03-01 11:50", mac="MAC2", tags=("Pet",)), event("c", "2026-03-01 09:00", mac="MAC2")])
    listed = r.watch.get_events("Backyard", 60, 20)["events"]
    assert [(e["event_id"], e["camera"], e["wyze_tags"]) for e in listed] == [("b", "Backyard", ["Pet"])]


# -- memory: timeline and search -----------------------------------------------


def test_timeline_tells_the_story_of_a_visit_and_hides_quiet_ones(rig) -> None:
    r = rig([event("quiet", "2026-03-01 11:46", mac="MAC2"), event("a", "2026-03-01 11:50"), event("b", "2026-03-01 11:54")])
    r.detector.script = [seen(summary="An empty yard."), seen(people=1, summary="A courier walks up."), seen(packages=1, summary="A box on the step.")]
    r.watch.poll(10)

    timeline = r.watch.timeline(None, 24, include_quiet=False)

    (visit,) = timeline["incidents"]
    assert (visit["camera"], visit["stills"], visit["alerts"]) == ("Front Door", 2, ["person", "package"])
    assert visit["changes"] == ["person arrived", "person left", "package appeared"]
    assert timeline["quiet_incidents_hidden"] == 1
    assert len(r.watch.timeline(None, 24, include_quiet=True)["incidents"]) == 2
    assert r.watch.timeline("Backyard", 24, include_quiet=False)["incidents"] == []


def test_search_finds_remembered_stills_by_what_was_in_them(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50", mac="MAC2"), event("b", "2026-03-01 11:54")])
    r.detector.script = [seen(pets=("dog",), summary="A brown dog plays in the yard."), seen(vehicles=1, summary="A white van is parked outside.")]
    r.watch.poll(10)

    assert [m["event_id"] for m in r.watch.search("dog playing in the yard", 24, 10)["matches"]] == ["a"]
    assert [m["camera"] for m in r.watch.search("white van", 24, 10)["matches"]] == ["Front Door"]


# -- custom checks -------------------------------------------------------------


def test_watch_rules_are_asked_of_the_right_cameras_and_raise_alerts(rig) -> None:
    r = rig([event("front", "2026-03-01 11:50"), event("yard", "2026-03-01 11:52", mac="MAC2")])
    r.watch.add_rule("a car is in the driveway", None)
    gate = r.watch.add_rule("  the gate\nis open ", "Backyard")
    assert gate == {"id": gate["id"], "rule": "the gate is open", "camera": "Backyard"}
    r.detector.true_statements = {2: {"the gate is open"}}

    front, yard = r.watch.watch(5)["new_events"]

    assert r.detector.asked == [["a car is in the driveway"], ["a car is in the driveway", "the gate is open"]]
    assert front["alerts"] == []
    assert yard["alerts"] == [{"type": "rule", "reason": "the gate is open", "priority": "normal", "rule_id": gate["id"]}]


def test_watch_rules_can_be_listed_and_removed(rig) -> None:
    r = rig()
    first = r.watch.add_rule("a car is in the driveway", None)
    r.watch.add_rule("the gate is open", "Backyard")
    assert [(rule["rule"], rule["camera"]) for rule in r.watch.list_rules()["rules"]] == [
        ("a car is in the driveway", "all cameras"), ("the gate is open", "Backyard"),
    ]
    r.watch.remove_rule(first["id"])
    assert [rule["rule"] for rule in r.watch.list_rules()["rules"]] == ["the gate is open"]
    with pytest.raises(WatchError, match="no watch rule"):
        r.watch.remove_rule(first["id"])


def test_the_number_of_watch_rules_is_capped(rig) -> None:
    r = rig()
    for n in range(MAX_RULES):
        r.watch.add_rule(f"statement number {n} is true", None)
    with pytest.raises(WatchError, match="Remove one"):
        r.watch.add_rule("one more thing", None)


# -- telling the user ----------------------------------------------------------


def test_a_text_is_sent_when_something_starts_and_not_for_every_still(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50"), event("b", "2026-03-01 11:52")])
    r.secrets.save_notify({"sms_phone": PHONE})
    r.detector.result = PERSON
    r.watch.poll(10)

    assert r.watch.notify_pending() == 1
    assert r.watch.notify_pending() == 0
    (text,) = r.outbox.bodies("textbelt.com")
    assert text["phone"] == PHONE
    assert text["message"] == "Person seen at Front Door: A person stands at the door. (11:50 AM)"


def test_nothing_is_sent_for_quiet_stills(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50"), event("b", "2026-03-01 11:52", mac="MAC2")])
    r.secrets.save_notify({"sms_phone": PHONE})
    r.detector.script = [seen(), seen(pets=("cat",), summary="A cat on the fence.")]
    r.watch.poll(10)
    assert r.watch.notify_pending() == 0
    assert r.outbox.requests == []


def test_a_camera_can_be_told_what_it_may_text_about(rig) -> None:
    r = rig([event("street", "2026-03-01 11:50", mac="MAC2"), event("door", "2026-03-01 11:51")])
    r.secrets.save_notify({"sms_phone": PHONE})
    r.detector.result = PERSON

    chosen = r.watch.set_camera_alerts("Backyard", ["intruder", "package_taken"])
    assert chosen == {"camera": "Backyard", "alerts_sent_to_phone": ["intruder", "package_taken"]}
    r.watch.poll(10)
    assert r.watch.notify_pending() == 1
    assert [body["message"].split(":")[0] for body in r.outbox.bodies("textbelt.com")] == ["Alert settings changed", "Person seen at Front Door"]

    by_name = {cam["name"]: cam["alerts_sent_to_phone"] for cam in r.restart().list_cameras()["cameras"]}
    assert by_name["Backyard"] == ["intruder", "package_taken"]
    assert "person" in by_name["Front Door"]

    assert "person" in r.watch.set_camera_alerts("Backyard", None)["alerts_sent_to_phone"], "omitting restores the default"
    assert r.watch.set_camera_alerts("Backyard", [])["alerts_sent_to_phone"] == []
    with pytest.raises(WatchError, match="burglar"):
        r.watch.set_camera_alerts("Backyard", ["burglar"])


def test_old_news_is_not_texted_when_notifications_are_set_up_later(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50")])
    r.detector.result = PERSON
    r.watch.poll(10)
    assert r.watch.notify_pending() == 0, "no channel yet: nothing claimed, nothing sent"

    r.secrets.save_notify({"sms_phone": PHONE})
    r.clock.set("2026-03-01 13:00")
    assert r.watch.notify_pending() == 0
    assert r.outbox.requests == []


def test_a_failed_text_is_retried_on_the_next_round(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50")])
    r.secrets.save_notify({"sms_phone": PHONE})
    r.detector.result = PERSON
    r.outbox.fail.add("textbelt.com")
    watcher = Watcher(r.watch, 60)

    assert watcher.cycle() == 0
    r.outbox.fail.clear()
    assert watcher.cycle() == 1
    assert len(r.outbox.bodies("textbelt.com")) == 2


def test_the_watcher_survives_being_signed_out_and_resumes(rig) -> None:
    r = rig([event("a", "2026-03-01 11:50")])
    r.secrets.save_notify({"ntfy_topic": "camsentry-secret-topic"})
    r.detector.result = PERSON
    working_events = r.wyze.events

    def signed_out(**kwargs):
        raise NotLoggedIn("Not connected to Wyze. Run `camsentry-mcp login` in a terminal.")

    r.wyze.events = signed_out
    watcher = Watcher(r.watch, 60)
    assert watcher.cycle() == 0

    r.wyze.events = working_events
    assert watcher.cycle() == 1


def test_the_chat_model_can_text_the_user_but_only_so_often(rig) -> None:
    r = rig()
    r.secrets.save_notify({"sms_phone": PHONE})

    first = r.watch.notify_user("  The package you asked about\nhas arrived.  ")
    assert first == {"sent_via": ["sms"], "remaining_this_hour": MANUAL_TEXTS_PER_HOUR - 1}
    assert r.outbox.bodies("textbelt.com")[0]["message"] == "Camera assistant: The package you asked about has arrived."

    for _ in range(MANUAL_TEXTS_PER_HOUR - 1):
        r.watch.notify_user("again")
    with pytest.raises(WatchError, match="Limit reached"):
        r.watch.notify_user("once more")

    r.clock.set("2026-03-01 13:01")
    assert r.restart().notify_user("an hour later")["sent_via"] == ["sms"]


def test_the_user_is_told_whenever_a_model_changes_what_they_are_alerted_about(rig) -> None:
    r = rig()
    r.secrets.save_notify({"sms_phone": PHONE})
    rule = r.watch.add_rule("the gate is open", None)

    r.watch.set_armed("off")
    r.watch.set_armed("off")
    r.watch.set_camera_alerts("Backyard", [])
    r.watch.remove_rule(rule["id"])

    assert [body["message"] for body in r.outbox.bodies("textbelt.com")] == [
        "Alert settings changed: Arming mode is now 'off' (was 'schedule').",
        "Alert settings changed: Backyard now texts you about: nothing.",
        "Alert settings changed: Watch rule removed: the gate is open.",
    ]


def test_settings_still_change_when_the_announcement_cannot_be_delivered(rig) -> None:
    r = rig()
    r.secrets.save_notify({"sms_phone": PHONE})
    r.outbox.fail.add("textbelt.com")
    assert r.watch.set_armed("on")["mode"] == "on"


def test_texting_without_a_destination_tells_the_model_what_the_user_must_do(rig) -> None:
    with pytest.raises(WatchError, match="camsentry-mcp setup"):
        rig().watch.notify_user("hello")


# -- through the MCP tool layer ------------------------------------------------


def call(watch: CameraWatch, tool: str, arguments: dict):
    return asyncio.run(build_server(watch).call_tool(tool, arguments))


def test_tool_returns_the_still_as_image_content(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55")])
    meta, image = call(r.watch, "view_camera", {"camera": "Front Door"}).content
    assert json.loads(meta.text)["event_id"] == "e1"
    assert (image.type, image.mime_type) == ("image", "image/jpeg")


def test_tool_results_reach_the_model_as_json_text(rig) -> None:
    r = rig([event("e1", "2026-03-01 11:55")])
    r.detector.result = PERSON
    result = call(r.watch, "watch_cameras", {})
    assert json.loads(result.content[0].text)["new_events"][0]["detection"]["summary"] == "A person stands at the door."


def test_tool_shows_the_login_hint_to_the_model_when_signed_out(rig) -> None:
    r = rig()

    def signed_out():
        raise NotLoggedIn("Not connected to Wyze. Run `camsentry-mcp login` in a terminal.")

    r.wyze.cameras = signed_out
    with pytest.raises(ToolError, match="camsentry-mcp login"):
        call(r.watch, "list_cameras", {})


def test_tool_rejects_out_of_range_arguments(rig) -> None:
    with pytest.raises(ToolError):
        call(rig().watch, "watch_cameras", {"max_events": 500})
    with pytest.raises(ToolError):
        call(rig().watch, "set_armed", {"mode": "maybe"})

from __future__ import annotations

from typing import Any

from conftest import at, seen
from camsentry_mcp.config import ArmedHours
from camsentry_mcp.policy import ArmState, Assessment, assess
from camsentry_mcp.store import Rule, Store
from camsentry_mcp.vision import Detection


def kinds(result: Assessment) -> list[str]:
    return [alert["type"] for alert in result.alerts]


def still(when: str, detection: Detection | None, previous: dict[str, Any] | None = None, *, armed: bool = False,
          tags: tuple[str, ...] = (), rules: tuple[Rule, ...] = ()) -> tuple[Assessment, dict[str, Any]]:
    """Assess one still and return the result plus the report a later still would be compared with."""
    result = assess(detection, tags, armed, at(when), previous, rules)
    report = {
        "time": at(when).isoformat(timespec="seconds"),
        "detection": detection.to_dict() if detection else None,
        "alerts": result.alerts,
        "person_since": result.person_since.isoformat(timespec="seconds") if result.person_since else None,
    }
    return result, report


def test_person_while_disarmed_is_not_an_intruder() -> None:
    result, _ = still("2026-03-01 12:00", seen(people=2))
    assert kinds(result) == ["person"]
    assert result.alerts[0]["priority"] == "normal"


def test_person_while_armed_is_an_urgent_intruder() -> None:
    result, _ = still("2026-03-01 12:00", seen(people=1), armed=True)
    assert kinds(result) == ["intruder", "person"]
    assert result.alerts[0]["priority"] == "urgent"


def test_suspicious_person_is_an_intruder_even_when_disarmed() -> None:
    result, _ = still("2026-03-01 12:00", seen(people=1, suspicious=True, reason="testing the door handle"))
    assert kinds(result) == ["intruder", "person"]
    assert "testing the door handle" in result.alerts[0]["reason"]


def test_suspicious_flag_without_a_person_raises_no_intruder_alert() -> None:
    assert kinds(still("2026-03-01 12:00", seen(suspicious=True, reason="odd shadow"))[0]) == []


def test_pets_and_vehicles_are_low_priority_and_never_intruders() -> None:
    result, _ = still("2026-03-01 12:00", seen(pets=("dog",), vehicles=1), armed=True)
    assert [(a["type"], a["priority"]) for a in result.alerts] == [("vehicle", "low"), ("pet", "low")]


def test_wyze_tags_raise_alerts_when_the_local_model_saw_nothing_or_did_not_run() -> None:
    assert kinds(still("2026-03-01 12:00", seen(), armed=True, tags=("Person", "Pet", "Package", "Vehicle"))[0]) == [
        "intruder", "person", "package", "vehicle", "pet",
    ]
    assert kinds(still("2026-03-01 12:00", None, armed=True, tags=("Person",))[0]) == ["intruder", "person"]


def test_person_in_consecutive_stills_becomes_lingering_and_is_announced_once() -> None:
    first, report = still("2026-03-01 12:00", seen(people=1))
    assert (first.new_alerts, first.changes) == (["person"], ["person arrived"])

    second, report = still("2026-03-01 12:04", seen(people=1), report)
    assert kinds(second) == ["lingering", "person"]
    assert "4 minutes" in second.alerts[0]["reason"]
    assert second.new_alerts == ["lingering"], "the person was already announced"
    assert second.changes == []

    third, _ = still("2026-03-01 12:08", seen(people=1), report)
    assert "8 minutes" in third.alerts[0]["reason"]
    assert third.new_alerts == [], "nothing new: no further text"


def test_person_seen_again_after_a_long_gap_is_a_new_visit_not_lingering() -> None:
    _, report = still("2026-03-01 12:00", seen(people=1))
    later, _ = still("2026-03-01 12:30", seen(people=1), report)
    assert kinds(later) == ["person"]
    assert (later.new_alerts, later.changes) == (["person"], ["person arrived"])


def test_person_leaving_is_noted() -> None:
    _, report = still("2026-03-01 12:00", seen(people=1))
    after, _ = still("2026-03-01 12:03", seen(), report)
    assert (kinds(after), after.changes) == ([], ["person left"])


def test_package_is_announced_when_it_appears_and_not_again_while_it_sits_there() -> None:
    _, empty = still("2026-03-01 09:00", seen())
    arrived, report = still("2026-03-01 10:00", seen(packages=1), empty)
    assert (arrived.new_alerts, arrived.changes) == (["package"], ["package appeared"])

    hours_later, _ = still("2026-03-01 15:00", seen(packages=1), report)
    assert kinds(hours_later) == ["package"]
    assert (hours_later.new_alerts, hours_later.changes) == ([], [])


def test_package_gone_after_a_person_was_there_is_an_urgent_alert() -> None:
    _, report = still("2026-03-01 10:00", seen(packages=1))
    _, report = still("2026-03-01 14:00", seen(people=1, packages=1), report)
    gone, _ = still("2026-03-01 14:03", seen(), report)
    assert kinds(gone) == ["package_taken"]
    assert gone.alerts[0]["priority"] == "urgent"
    assert gone.new_alerts == ["package_taken"]
    assert gone.changes == ["person left", "package gone"]


def test_package_gone_with_nobody_seen_is_only_a_note() -> None:
    _, report = still("2026-03-01 10:00", seen(packages=1))
    gone, _ = still("2026-03-01 14:00", seen(), report)
    assert (kinds(gone), gone.changes) == ([], ["package gone"])


def test_a_still_the_model_could_not_read_does_not_look_like_a_missing_package() -> None:
    _, report = still("2026-03-01 10:00", seen(people=1, packages=1))
    unread, _ = still("2026-03-01 10:03", None, report)
    assert (kinds(unread), unread.changes) == ([], [])


def test_vehicle_arrival_and_departure_are_noted() -> None:
    _, report = still("2026-03-01 10:00", seen())
    arrived, report = still("2026-03-01 11:00", seen(vehicles=1), report)
    assert (arrived.new_alerts, arrived.changes) == (["vehicle"], ["vehicle arrived"])
    left, _ = still("2026-03-01 13:00", seen(), report)
    assert left.changes == ["vehicle left"]


def test_custom_rule_alerts_once_while_it_stays_true_and_again_after_it_stopped() -> None:
    gate = Rule(7, None, "the gate is open")
    opened, report = still("2026-03-01 10:00", seen(), rules=(gate,))
    assert opened.alerts == [{"type": "rule", "reason": "the gate is open", "priority": "normal", "rule_id": 7}]
    assert opened.new_alerts == ["rule:7"]

    still_open, report = still("2026-03-01 12:00", seen(), report, rules=(gate,))
    assert still_open.new_alerts == []

    _, report = still("2026-03-01 13:00", seen(), report)
    reopened, _ = still("2026-03-01 14:00", seen(), report, rules=(gate,))
    assert reopened.new_alerts == ["rule:7"]


def test_two_rules_are_tracked_separately() -> None:
    gate, light = Rule(1, None, "the gate is open"), Rule(2, None, "the porch light is on")
    _, report = still("2026-03-01 10:00", seen(), rules=(gate,))
    both, _ = still("2026-03-01 11:00", seen(), report, rules=(gate, light))
    assert both.new_alerts == ["rule:2"]


def test_schedule_mode_follows_armed_hours(memory: Store) -> None:
    arm = ArmState(ArmedHours.parse("22:00-06:00"), memory)
    assert arm.is_armed(at("2026-03-01 23:30"))
    assert not arm.is_armed(at("2026-03-01 12:00"))


def test_without_armed_hours_schedule_mode_is_never_armed(memory: Store) -> None:
    assert not ArmState(None, memory).is_armed(at("2026-03-01 03:00"))


def test_manual_modes_override_the_schedule_and_survive_a_restart(memory: Store) -> None:
    hours = ArmedHours.parse("22:00-06:00")
    ArmState(hours, memory).set_mode("on")
    after_restart = ArmState(hours, memory)
    assert after_restart.is_armed(at("2026-03-01 12:00"))

    after_restart.set_mode("off")
    assert not ArmState(hours, memory).is_armed(at("2026-03-01 23:30"))

    after_restart.set_mode("schedule")
    assert ArmState(hours, memory).is_armed(at("2026-03-01 23:30"))

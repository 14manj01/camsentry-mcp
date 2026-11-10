from conftest import at
from wyze_cam_mcp.config import ArmedHours
from wyze_cam_mcp.policy import ArmState, build_alerts
from wyze_cam_mcp.vision import Detection


def detection(people: int = 0, pets: tuple[str, ...] = (), packages: int = 0, suspicious: bool = False, reason: str = "") -> Detection:
    return Detection(people, pets, packages, suspicious, reason, "scene")


def kinds(alerts: list[dict[str, str]]) -> list[str]:
    return [alert["type"] for alert in alerts]


def test_person_while_disarmed_is_not_an_intruder() -> None:
    assert kinds(build_alerts(detection(people=2), (), armed=False)) == ["person"]


def test_person_while_armed_is_an_intruder() -> None:
    assert kinds(build_alerts(detection(people=1), (), armed=True)) == ["person", "intruder"]


def test_suspicious_person_is_an_intruder_even_when_disarmed() -> None:
    alerts = build_alerts(detection(people=1, suspicious=True, reason="testing the door handle"), (), armed=False)
    assert kinds(alerts) == ["person", "intruder"]
    assert "testing the door handle" in alerts[1]["reason"]


def test_suspicious_flag_without_a_person_raises_no_intruder_alert() -> None:
    assert kinds(build_alerts(detection(people=0, suspicious=True, reason="odd shadow"), (), armed=False)) == []


def test_armed_empty_scene_raises_nothing() -> None:
    assert build_alerts(detection(), (), armed=True) == []


def test_pets_and_packages_are_reported_without_intruder_while_armed() -> None:
    alerts = build_alerts(detection(pets=("dog", "cat"), packages=2), (), armed=True)
    assert kinds(alerts) == ["pet", "package"]
    assert alerts[0]["reason"] == "dog, cat"


def test_wyze_tags_alone_raise_alerts_when_the_local_model_saw_nothing() -> None:
    alerts = build_alerts(detection(), ("Person", "Pet", "Package"), armed=True)
    assert kinds(alerts) == ["person", "pet", "package", "intruder"]


def test_wyze_tags_still_count_when_no_local_detection_ran() -> None:
    assert kinds(build_alerts(None, ("Person",), armed=True)) == ["person", "intruder"]
    assert kinds(build_alerts(None, ("Vehicle",), armed=True)) == []


def test_schedule_mode_follows_armed_hours() -> None:
    arm = ArmState(ArmedHours.parse("22:00-06:00"))
    assert arm.is_armed(at("2026-03-01 23:30"))
    assert not arm.is_armed(at("2026-03-01 12:00"))


def test_without_armed_hours_schedule_mode_is_never_armed() -> None:
    assert not ArmState(None).is_armed(at("2026-03-01 03:00"))


def test_manual_modes_override_the_schedule_and_can_be_reverted() -> None:
    arm = ArmState(ArmedHours.parse("22:00-06:00"))
    arm.set_mode("on")
    assert arm.is_armed(at("2026-03-01 12:00"))
    arm.set_mode("off")
    assert not arm.is_armed(at("2026-03-01 23:30"))
    arm.set_mode("schedule")
    assert arm.is_armed(at("2026-03-01 23:30"))

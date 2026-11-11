from __future__ import annotations

from conftest import at, seen
from camsentry_mcp.timeline import build_incidents, is_notable, search_reports


def report(event_id: str, when: str, summary: str = "An empty porch.", *, camera: str = "Front Door", camera_id: str = "MAC1",
           alerts: tuple[str, ...] = (), changes: tuple[str, ...] = (), **counts) -> dict:
    return {
        "event_id": event_id,
        "camera": camera,
        "camera_id": camera_id,
        "time": at(when).isoformat(timespec="seconds"),
        "wyze_tags": [],
        "detection": seen(summary=summary, **counts).to_dict(),
        "alerts": [{"type": kind, "reason": kind, "priority": "normal"} for kind in alerts],
        "changes": list(changes),
    }


def test_stills_close_together_on_one_camera_form_one_visit() -> None:
    reports = [
        report("a", "2026-03-01 10:00", "A courier walks up.", people=1, alerts=("person",), changes=("person arrived",)),
        report("b", "2026-03-01 10:04", "A courier sets down a box.", people=1, packages=1, alerts=("person", "package"), changes=("package appeared",)),
        report("c", "2026-03-01 10:08", "A box sits on the porch.", packages=1, alerts=("package",), changes=("person left",)),
        report("d", "2026-03-01 15:00", "A dog sniffs the box.", pets=("dog",), packages=1, alerts=("package", "pet")),
    ]

    later, visit = build_incidents(reports)

    assert (visit["stills"], visit["people"], visit["packages"]) == (3, 1, 1)
    assert (visit["start"], visit["end"]) == (reports[0]["time"], reports[2]["time"])
    assert visit["alerts"] == ["person", "package"]
    assert visit["changes"] == ["person arrived", "package appeared", "person left"]
    assert visit["story"] == [
        "10:00 A courier walks up. [person arrived]",
        "10:04 A courier sets down a box. [package appeared]",
        "10:08 A box sits on the porch. [person left]",
    ]
    assert (later["stills"], later["pets"], later["event_ids"]) == (1, ["dog"], ["d"])


def test_cameras_are_never_merged_into_one_visit() -> None:
    reports = [
        report("a", "2026-03-01 10:00", people=1, alerts=("person",)),
        report("b", "2026-03-01 10:01", camera="Backyard", camera_id="MAC2", people=1, alerts=("person",)),
    ]
    assert sorted(incident["camera"] for incident in build_incidents(reports)) == ["Backyard", "Front Door"]


def test_long_visits_keep_the_beginning_and_end_of_the_story() -> None:
    reports = [report(f"e{n}", f"2026-03-01 10:{n * 3:02d}", f"Still number {n}.") for n in range(12)]
    (visit,) = build_incidents(reports)
    assert visit["stills"] == 12
    assert len(visit["story"]) == 8
    assert visit["story"][0].endswith("Still number 0.") and visit["story"][-1].endswith("Still number 11.")
    assert visit["story"][4] == "... 5 more stills ..."


def test_a_visit_is_notable_when_something_happened_not_because_a_car_is_parked() -> None:
    empty, parked, arrived, person = build_incidents([
        report("a", "2026-03-01 09:00"),
        report("b", "2026-03-01 10:00", vehicles=1, pets=("cat",), alerts=("vehicle", "pet")),
        report("c", "2026-03-01 12:00", vehicles=1, alerts=("vehicle",), changes=("vehicle arrived",)),
        report("d", "2026-03-01 14:00", people=1, vehicles=1, alerts=("person", "vehicle")),
    ])[::-1]
    assert [is_notable(visit) for visit in (empty, parked, arrived, person)] == [False, False, True, True]


REPORTS = [
    report("dog-yard", "2026-03-01 09:00", "A brown dog is playing in the yard.", camera="Backyard", camera_id="MAC2", pets=("dog",), alerts=("pet",)),
    report("dogs-porch", "2026-03-01 10:00", "Two dogs sleep on the porch.", pets=("dog", "dog"), alerts=("pet",)),
    report("van", "2026-03-01 11:00", "A white van is parked in the driveway.", vehicles=1, alerts=("vehicle",)),
    report("yard-empty", "2026-03-01 12:00", "The yard is empty.", camera="Backyard", camera_id="MAC2"),
    report("taken", "2026-03-01 13:00", "A person walks away.", people=1, alerts=("person", "package_taken")),
]


def found(query: str, limit: int = 10) -> list[str]:
    return [match["event_id"] for match in search_reports(REPORTS, query, limit)]


def test_search_prefers_stills_matching_every_word() -> None:
    assert found("Show me the dog playing in the yard") == ["dog-yard"]


def test_search_falls_back_to_partial_matches_newest_first() -> None:
    assert found("dog on the roof") == ["dogs-porch", "dog-yard"]


def test_search_matches_plurals_camera_names_and_alert_names() -> None:
    assert found("dogs") == ["dogs-porch", "dog-yard"]
    assert found("backyard") == ["yard-empty", "dog-yard"]
    assert found("package taken") == ["taken"]


def test_search_respects_the_limit_and_ignores_filler_words() -> None:
    assert found("dog", limit=1) == ["dogs-porch"]
    assert found("show me what is there") == []
    assert found("helicopter") == []

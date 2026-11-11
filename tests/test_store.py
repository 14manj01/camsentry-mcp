from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import at
from camsentry_mcp.store import HISTORY_DAYS, Store

NOW = at("2026-03-01 12:00")
DAY = timedelta(hours=24)


def save(db: Store, event_id: str, when: str, camera_id: str = "MAC1", analysed: bool = True) -> None:
    report = {"event_id": event_id, "time": at(when).isoformat(), "detection": {"people": 0} if analysed else None}
    db.save_report(event_id, camera_id, at(when), report)


def ids(reports: list[dict]) -> list[str]:
    return [report["event_id"] for report in reports]


def test_each_report_is_handed_out_once_oldest_first(memory: Store) -> None:
    save(memory, "b", "2026-03-01 11:30")
    save(memory, "a", "2026-03-01 11:00")
    save(memory, "c", "2026-03-01 11:45")

    first, more = memory.take_unreported(NOW, 2, NOW - DAY)
    assert (ids(first), more) == (["a", "b"], True)
    second, more = memory.take_unreported(NOW, 2, NOW - DAY)
    assert (ids(second), more) == (["c"], False)
    assert memory.take_unreported(NOW, 2, NOW - DAY) == ([], False)


def test_reporting_to_the_model_and_notifying_the_phone_are_tracked_separately(memory: Store) -> None:
    save(memory, "a", "2026-03-01 11:55")
    assert ids(memory.take_unnotified(NOW, 5, NOW - DAY)) == ["a"]
    assert ids(memory.take_unreported(NOW, 5, NOW - DAY)[0]) == ["a"]


def test_stale_reports_are_skipped_for_good(memory: Store) -> None:
    save(memory, "old", "2026-03-01 09:00")
    save(memory, "recent", "2026-03-01 11:50")
    half_hour_ago = NOW - timedelta(minutes=30)
    assert ids(memory.take_unnotified(NOW, 5, half_hour_ago)) == ["recent"]
    assert memory.take_unnotified(NOW, 5, NOW - DAY) == [], "the old one must not resurface with a wider window"


def test_two_processes_sharing_the_file_never_deliver_the_same_report(tmp_path: Path) -> None:
    server, watcher = Store(tmp_path / "db"), Store(tmp_path / "db")
    for n in range(6):
        save(server, f"e{n}", f"2026-03-01 11:5{n}")

    delivered = ids(server.take_unnotified(NOW, 3, NOW - DAY)) + ids(watcher.take_unnotified(NOW, 3, NOW - DAY))
    delivered += ids(server.take_unnotified(NOW, 3, NOW - DAY)) + ids(watcher.take_unnotified(NOW, 3, NOW - DAY))

    assert sorted(delivered) == [f"e{n}" for n in range(6)]
    server.close()
    watcher.close()


def test_released_notification_is_offered_again(memory: Store) -> None:
    save(memory, "a", "2026-03-01 11:55")
    memory.take_unnotified(NOW, 5, NOW - DAY)
    memory.release_notification("a")
    assert ids(memory.take_unnotified(NOW, 5, NOW - DAY)) == ["a"]


def test_analysing_a_still_again_keeps_its_delivery_marks(memory: Store) -> None:
    save(memory, "a", "2026-03-01 11:55")
    memory.take_unreported(NOW, 5, NOW - DAY)
    save(memory, "a", "2026-03-01 11:55")
    assert memory.take_unreported(NOW, 5, NOW - DAY) == ([], False)


def test_previous_report_is_the_same_cameras_last_analysed_still_within_the_window(memory: Store) -> None:
    save(memory, "other-camera", "2026-03-01 11:58", camera_id="MAC2")
    save(memory, "unreadable", "2026-03-01 11:57", analysed=False)
    save(memory, "wanted", "2026-03-01 11:50")
    save(memory, "older", "2026-03-01 11:40")
    save(memory, "later", "2026-03-01 12:30")

    assert memory.previous_report("MAC1", NOW, DAY)["event_id"] == "wanted"
    assert memory.previous_report("MAC1", NOW, timedelta(minutes=5)) is None
    assert memory.previous_report("MAC3", NOW, DAY) is None


def test_history_survives_reopening_and_old_history_is_pruned(tmp_path: Path) -> None:
    db = Store(tmp_path / "db")
    save(db, "ancient", "2026-01-01 12:00")
    save(db, "recent", "2026-03-01 11:00")
    db.set_setting("arm_mode", "on")
    db.close()

    reopened = Store(tmp_path / "db")
    assert reopened.get_setting("arm_mode") == "on"
    reopened.prune(NOW)
    assert ids(reopened.reports_since(NOW - timedelta(days=HISTORY_DAYS * 3))) == ["recent"]
    reopened.close()


def test_reports_since_filters_by_camera_and_keeps_the_newest_when_limited(memory: Store) -> None:
    for n in range(5):
        save(memory, f"e{n}", f"2026-03-01 11:0{n}")
    save(memory, "yard", "2026-03-01 11:30", camera_id="MAC2")

    assert ids(memory.reports_since(NOW - DAY, "MAC2")) == ["yard"]
    assert ids(memory.reports_since(NOW - DAY, "MAC1", limit=2)) == ["e3", "e4"]


def test_forget_removes_stills_but_keeps_rules(memory: Store) -> None:
    save(memory, "a", "2026-03-01 11:55")
    memory.add_rule("the gate is open", None, NOW)
    assert memory.forget() == 1
    assert memory.reports_since(NOW - DAY) == []
    assert [rule.text for rule in memory.rules()] == ["the gate is open"]


def test_rules_can_be_added_scoped_and_removed(memory: Store) -> None:
    everywhere = memory.add_rule("a car is in the driveway", None, NOW)
    scoped = memory.add_rule("the gate is open", "MAC2", NOW)
    assert [(rule.id, rule.camera_id) for rule in memory.rules()] == [(everywhere.id, None), (scoped.id, "MAC2")]

    assert memory.remove_rule(everywhere.id) is True
    assert memory.remove_rule(everywhere.id) is False
    assert [rule.text for rule in memory.rules()] == ["the gate is open"]

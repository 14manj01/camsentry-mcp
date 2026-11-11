from datetime import time
from pathlib import Path

import pytest

from camsentry_mcp.config import ALERT_TYPES, DEFAULT_NOTIFY, ArmedHours, ConfigError, Settings


@pytest.mark.parametrize(
    ("moment", "armed"),
    [("22:00", True), ("23:59", True), ("02:00", True), ("05:59", True), ("06:00", False), ("12:00", False), ("21:59", False)],
)
def test_armed_hours_spanning_midnight(moment: str, armed: bool) -> None:
    assert ArmedHours.parse("22:00-06:00").contains(time.fromisoformat(moment)) is armed


@pytest.mark.parametrize(("moment", "armed"), [("09:00", True), ("16:59", True), ("17:00", False), ("08:59", False)])
def test_armed_hours_within_one_day(moment: str, armed: bool) -> None:
    assert ArmedHours.parse("09:00-17:00").contains(time.fromisoformat(moment)) is armed


@pytest.mark.parametrize("text", ["22:00", "10pm-6am", "25:00-06:00", "22:00-06:00-07:00"])
def test_malformed_armed_hours_are_rejected(text: str) -> None:
    with pytest.raises(ConfigError):
        ArmedHours.parse(text)


def test_nothing_needs_to_be_configured() -> None:
    settings = Settings.from_env({})
    assert settings.vision_model is None, "the vision model is chosen automatically"
    assert settings.notify_types == DEFAULT_NOTIFY
    assert settings.poll_seconds == 60
    assert settings.data_dir.name == "camsentry-mcp"


@pytest.mark.parametrize("url", ["http://localhost:1234/v1", "http://127.0.0.1:11434/v1", "http://[::1]:8080/v1"])
def test_loopback_vision_endpoints_are_accepted(url: str) -> None:
    assert Settings.from_env({"CAMSENTRY_VISION_URL": url}).vision_url == url


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://192.168.1.20:11434/v1", "http://localhost.evil.com/v1"])
def test_remote_vision_endpoint_needs_explicit_opt_in(url: str) -> None:
    with pytest.raises(ConfigError, match="leave"):
        Settings.from_env({"CAMSENTRY_VISION_URL": url})
    allowed = Settings.from_env({"CAMSENTRY_VISION_URL": url, "CAMSENTRY_ALLOW_REMOTE_VISION": "1"})
    assert allowed.vision_url == url


def test_urls_with_embedded_credentials_are_rejected() -> None:
    with pytest.raises(ConfigError, match="credentials"):
        Settings.from_env({"CAMSENTRY_NTFY_SERVER": "https://admin:hunter2@ntfy.example"})


def test_quiet_alert_types_are_off_by_default() -> None:
    assert DEFAULT_NOTIFY == set(ALERT_TYPES) - {"pet", "vehicle"}


def test_notified_alert_types_can_be_chosen() -> None:
    assert Settings.from_env({"CAMSENTRY_NOTIFY": "Intruder, pet ,"}).notify_types == {"intruder", "pet"}
    assert Settings.from_env({"CAMSENTRY_NOTIFY": ""}).notify_types == set()
    with pytest.raises(ConfigError, match="burglar"):
        Settings.from_env({"CAMSENTRY_NOTIFY": "intruder,burglar"})


@pytest.mark.parametrize(("text", "seconds"), [("0", 0), ("15", 15), ("300", 300)])
def test_poll_interval_can_be_changed_or_switched_off(text: str, seconds: int) -> None:
    assert Settings.from_env({"CAMSENTRY_POLL_SECONDS": text}).poll_seconds == seconds


@pytest.mark.parametrize("text", ["5", "-60", "often"])
def test_poll_intervals_that_would_hammer_wyze_are_rejected(text: str) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"CAMSENTRY_POLL_SECONDS": text})


def test_data_directory_can_be_moved(tmp_path: Path) -> None:
    assert Settings.from_env({"CAMSENTRY_DATA_DIR": str(tmp_path)}).data_dir == tmp_path

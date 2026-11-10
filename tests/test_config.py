from datetime import time

import pytest

from wyze_cam_mcp.config import ArmedHours, ConfigError, Settings


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


@pytest.mark.parametrize("url", ["http://localhost:1234/v1", "http://127.0.0.1:11434/v1", "http://[::1]:8080/v1"])
def test_loopback_vision_endpoints_are_accepted(url: str) -> None:
    assert Settings.from_env({"WYZE_CAM_VISION_URL": url}).vision_url == url


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://192.168.1.20:11434/v1", "http://localhost.evil.com/v1"])
def test_remote_vision_endpoint_needs_explicit_opt_in(url: str) -> None:
    with pytest.raises(ConfigError, match="leave"):
        Settings.from_env({"WYZE_CAM_VISION_URL": url})
    allowed = Settings.from_env({"WYZE_CAM_VISION_URL": url, "WYZE_CAM_ALLOW_REMOTE_VISION": "1"})
    assert allowed.vision_url == url


def test_urls_with_embedded_credentials_are_rejected() -> None:
    with pytest.raises(ConfigError, match="credentials"):
        Settings.from_env({"WYZE_CAM_BRIDGE_URL": "http://admin:hunter2@192.168.1.5:5080"})

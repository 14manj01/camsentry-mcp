from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import requests
from wyze_sdk.errors import WyzeApiError
from wyze_sdk.models.devices import DeviceParser
from wyze_sdk.models.events import Event

from conftest import JPEG
from wyze_cam_mcp.credentials import SecretStore, Tokens
from wyze_cam_mcp.images import ImageError
from wyze_cam_mcp.server import quiet_library_logging
from wyze_cam_mcp.wyze import NotLoggedIn, WyzeCloud, WyzeError

EXPIRED = {"code": "2001", "msg": "AccessTokenError"}
NOW = datetime(2026, 3, 1, 12, 0).astimezone()


def raw_event(event_id: str, when: datetime, **extra: Any) -> Event:
    """An event exactly as wyze-sdk builds it from the Wyze API payload."""
    payload = {
        "device_mac": "MAC1",
        "event_id": event_id,
        "event_ts": int(when.timestamp() * 1000),
        "event_category": 1,
        "event_params": {},
        "event_value": "13",
        "file_list": [{"file_id": "f1", "type": 1, "status": 1, "url": f"https://img.example/{event_id}.jpg"}],
        "tag_list": [],
        "read_state": 0,
    }
    return Event(**(payload | extra))


class FakeEvents:
    def __init__(self, account: FakeAccount) -> None:
        self._account = account

    def list(self, *, device_ids, begin, end, limit, order_by):
        self._account.check_token()
        self._account.page_sizes.append(limit)
        matching = [e for e in self._account.events if begin.timestamp() * 1000 <= e.time <= end.timestamp() * 1000]
        return sorted(matching, key=lambda e: e.time, reverse=order_by == 2)[:limit]


class FakeAccount:
    """A fake Wyze backend: issues tokens, expires them, and serves devices and events."""

    def __init__(self) -> None:
        self.valid_access = "access-1"
        self.valid_refresh = "refresh-1"
        self.refresh_calls = 0
        self.devices: list[dict[str, Any]] = []
        self.events: list[Event] = []
        self.page_sizes: list[int] = []
        self.failure: Exception | None = None
        self._token = ""
        self._refresh = ""

    def client(self, *, token: str, refresh_token: str) -> FakeAccount:
        self._token, self._refresh = token, refresh_token
        return self

    def check_token(self) -> None:
        if self.failure:
            raise self.failure
        if self._token != self.valid_access:
            raise WyzeApiError("The access token has expired.", EXPIRED)

    def rotate(self) -> None:
        generation = int(self.valid_access.split("-")[1]) + 1
        self.valid_access, self.valid_refresh = f"access-{generation}", f"refresh-{generation}"

    def refresh_token(self) -> dict[str, Any]:
        self.refresh_calls += 1
        if self._refresh != self.valid_refresh:
            raise WyzeApiError("refresh token rejected", {"code": "2002"})
        self.rotate()
        return {"data": {"access_token": self.valid_access, "refresh_token": self.valid_refresh}}

    def devices_list(self) -> list[Any]:
        self.check_token()
        return [DeviceParser.parse(dict(device)) for device in self.devices]


@pytest.fixture
def account() -> FakeAccount:
    return FakeAccount()


def cloud(store: SecretStore, account: FakeAccount, handler=None) -> WyzeCloud:
    transport = httpx.MockTransport(handler or (lambda request: httpx.Response(200, content=JPEG)))

    class Client:
        def __init__(self, *, token: str, refresh_token: str) -> None:
            account.client(token=token, refresh_token=refresh_token)
            self.events = FakeEvents(account)

        def devices_list(self):
            return account.devices_list()

        def refresh_token(self):
            return account.refresh_token()

    return WyzeCloud(store, httpx.Client(transport=transport), client_factory=Client)


def test_without_stored_tokens_the_user_is_told_to_log_in(store: SecretStore, account: FakeAccount) -> None:
    with pytest.raises(NotLoggedIn, match="wyze-cam-mcp login"):
        cloud(store, account).cameras()


def test_expired_access_token_is_refreshed_and_rotated_tokens_are_persisted(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-0", "refresh-1"))
    account.devices = [{"mac": "MAC1", "nickname": "Porch", "product_model": "HL_CAM4", "product_type": "Camera"}]

    cameras = cloud(store, account).cameras()

    assert [camera.name for camera in cameras] == ["Porch"]
    assert store.load_tokens() == Tokens("access-2", "refresh-2")


def test_rejected_refresh_token_requires_a_new_login(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-0", "refresh-revoked"))
    with pytest.raises(NotLoggedIn, match="expired"):
        cloud(store, account).cameras()


def test_tokens_rotated_by_another_process_are_adopted_without_refreshing(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    wyze = cloud(store, account)
    wyze.cameras()

    # A second server process refreshes: Wyze invalidates the old pair and the keychain gets the new one.
    account.rotate()
    store.save_tokens(Tokens(account.valid_access, account.valid_refresh))

    wyze.cameras()
    assert account.refresh_calls == 0


def test_api_errors_do_not_leak_the_raw_response(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.failure = WyzeApiError("failed", {"code": "1001", "debug": "token=lvtx.SECRET"})
    with pytest.raises(WyzeError) as caught:
        cloud(store, account).cameras()
    assert "SECRET" not in str(caught.value)
    assert "1001" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_network_failure_is_reported_as_unreachable(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.failure = requests.ConnectionError("dns failure for api.wyzecam.com")
    with pytest.raises(WyzeError, match="Could not reach Wyze"):
        cloud(store, account).cameras()


def test_every_camera_type_device_is_listed_even_models_the_sdk_does_not_know(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.devices = [
        {"mac": "MAC1", "nickname": "Porch", "product_model": "WYZE_CAKP2JFUS", "product_type": "Camera", "conn_state": 1, "conn_state_ts": 1},
        {"mac": "MAC2", "nickname": "Yard", "product_model": "HL_FUTURE_CAM9", "product_type": "Camera", "conn_state": 0, "conn_state_ts": 1},
        {"mac": "MAC3", "nickname": "Lamp", "product_model": "WLPP1", "product_type": "Plug"},
    ]
    cameras = cloud(store, account).cameras()
    assert [(c.name, c.model, c.online) for c in cameras] == [
        ("Porch", "WYZE_CAKP2JFUS", True),
        ("Yard", "HL_FUTURE_CAM9", False),
    ]


def test_events_are_converted_from_sdk_models(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    when = NOW - timedelta(minutes=5)
    account.events = [
        raw_event("e1", when, tag_list=[101, 104]),
        raw_event("e2", when - timedelta(minutes=1), file_list=[{"file_id": "f", "type": 1, "url": "http://insecure.example/x.jpg"}]),
        raw_event("e3", when - timedelta(minutes=2), file_list=[{"file_id": "v", "type": 2, "url": "https://img.example/clip.mp4"}]),
    ]

    events = cloud(store, account).events(since=NOW - timedelta(hours=1), until=NOW)

    first, second, third = events
    assert (first.id, first.kind, first.wyze_tags) == ("e1", "Motion", ("Person", "Package"))
    assert first.time == when and first.time.tzinfo is not None
    assert first.image_url == "https://img.example/e1.jpg"
    assert second.image_url is None, "plain-http image URLs must never be fetched"
    assert third.image_url is None, "video files are not images"


def test_events_page_past_the_wyze_limit_of_twenty(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.events = [raw_event(f"e{i}", NOW - timedelta(minutes=i + 1)) for i in range(45)]

    events = cloud(store, account).events(since=NOW - timedelta(hours=2), until=NOW, limit=45)

    assert [e.id for e in events] == [f"e{i}" for i in range(45)]
    assert max(account.page_sizes) <= 20


def test_oldest_first_returns_the_earliest_events(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.events = [raw_event(f"e{i}", NOW - timedelta(minutes=i + 1)) for i in range(30)]

    events = cloud(store, account).events(since=NOW - timedelta(hours=2), until=NOW, limit=25, oldest_first=True)

    assert [e.id for e in events] == [f"e{i}" for i in range(29, 4, -1)]


def test_image_download_errors_never_contain_the_presigned_url(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.events = [raw_event("e1", NOW - timedelta(minutes=1), file_list=[{"file_id": "f", "type": 1, "url": "https://img.example/e1.jpg?X-Amz-Signature=SIGNED"}])]
    wyze = cloud(store, account, handler=lambda request: httpx.Response(403))
    (event,) = wyze.events(since=NOW - timedelta(hours=1), until=NOW)

    with pytest.raises(ImageError) as caught:
        wyze.event_image(event)
    assert "SIGNED" not in str(caught.value) and "403" in str(caught.value)
    assert "SIGNED" not in repr(event)


def test_non_image_downloads_are_rejected(store: SecretStore, account: FakeAccount) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.events = [raw_event("e1", NOW - timedelta(minutes=1))]
    wyze = cloud(store, account, handler=lambda request: httpx.Response(200, content=b"<html>login</html>"))
    (event,) = wyze.events(since=NOW - timedelta(hours=1), until=NOW)
    with pytest.raises(ImageError, match="not a JPEG"):
        wyze.event_image(event)


def test_sight_safe_image_host_only_serves_the_wyze_app_user_agent(store: SecretStore, account: FakeAccount) -> None:
    # prod-sight-safe-auth.wyze.com answers 401 "Access token is invalid" to any other client.
    def gateway(request: httpx.Request) -> httpx.Response:
        if not request.headers["user-agent"].startswith("okhttp/"):
            return httpx.Response(401, json={"error": "Access token is invalid."})
        return httpx.Response(200, content=JPEG)

    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.events = [raw_event("e1", NOW - timedelta(minutes=1))]
    wyze = cloud(store, account, handler=gateway)
    (event,) = wyze.events(since=NOW - timedelta(hours=1), until=NOW)

    assert wyze.event_image(event) == (JPEG, "image/jpeg")


def test_presigned_image_urls_are_not_written_to_the_log(store: SecretStore, account: FakeAccount, caplog: pytest.LogCaptureFixture) -> None:
    store.save_tokens(Tokens("access-1", "refresh-1"))
    account.events = [raw_event("e1", NOW - timedelta(minutes=1), file_list=[{"file_id": "f", "type": 1, "url": "https://img.example/e1.jpg?st=SIGNED"}])]
    wyze = cloud(store, account)
    (event,) = wyze.events(since=NOW - timedelta(hours=1), until=NOW)

    quiet_library_logging()
    with caplog.at_level(logging.INFO):  # the MCP SDK turns on INFO logging for the whole process
        wyze.event_image(event)

    assert "SIGNED" not in caplog.text

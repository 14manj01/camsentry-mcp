"""Read-only Wyze cloud access: cameras, motion events and event images.

Only session tokens are used here. Errors raised to callers are rewritten so that
no token, URL or raw API response can reach a log or a model.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import requests
from wyze_sdk import Client
from wyze_sdk.errors import WyzeApiError, WyzeClientError
from wyze_sdk.models.events import AiEventType, EventFileType

from .credentials import SecretStore, Tokens
from .images import ImageError, download_image

_PAGE = 20  # Wyze caps event queries at 20 per request.
# Wyze's image gateway (prod-sight-safe-auth.wyze.com) authorises by the signed URL alone, but answers
# 401 "Access token is invalid" unless the request looks like the Android app's HTTP client.
_IMAGE_HEADERS = {"User-Agent": "okhttp/4.9.3"}
_NOT_LOGGED_IN = "Not connected to Wyze. Run `wyze-cam-mcp login` in a terminal."
_SESSION_EXPIRED = "The Wyze session expired. Run `wyze-cam-mcp login` in a terminal."


class NotLoggedIn(Exception):
    """No usable Wyze session exists; the user has to run the login command."""


class WyzeError(Exception):
    """Wyze could not be reached or rejected a request."""


class _TokenExpired(Exception):
    pass


@dataclass(frozen=True)
class Camera:
    mac: str
    name: str
    model: str
    online: bool


@dataclass(frozen=True)
class CameraEvent:
    id: str
    mac: str
    time: datetime
    kind: str
    wyze_tags: tuple[str, ...]
    image_url: str | None = field(repr=False)


def _error_data(err: WyzeApiError) -> dict[str, Any]:
    return err.response if isinstance(err.response, dict) else {}


def _token_expired(err: WyzeApiError) -> bool:
    data = _error_data(err)
    return str(data.get("code")) == "2001" or data.get("msg") == "AccessTokenError"


def _event_time(value: Any) -> datetime | None:
    # The SDK annotates Event.time as datetime but passes Wyze's epoch milliseconds through.
    if isinstance(value, datetime):
        return value.astimezone()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).astimezone()
    return None


def _to_event(raw: Any) -> CameraEvent | None:
    when = _event_time(raw.time)
    if when is None or raw.id is None or raw.mac is None:
        return None
    try:
        alarm = raw.alarm_type
    except AttributeError:  # the SDK leaves it unset for alarm codes it cannot parse
        alarm = None
    tags = tuple(tag.description for tag in raw.tags if tag is not None and tag is not AiEventType.NOTHING)
    image_url = next(
        (f.url for f in raw.files if f.type is EventFileType.IMAGE and f.url and f.url.startswith("https://")),
        None,
    )
    return CameraEvent(
        id=str(raw.id),
        mac=raw.mac,
        time=when,
        kind=alarm.description if alarm is not None else "Unknown",
        wyze_tags=tags,
        image_url=image_url,
    )


class WyzeCloud:
    def __init__(
        self,
        store: SecretStore,
        http: httpx.Client,
        client_factory: Callable[..., Any] = Client,
    ) -> None:
        self._store = store
        self._http = http
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._tokens: Tokens | None = None
        self._client: Any = None

    def _adopt(self, tokens: Tokens) -> None:
        self._tokens = tokens
        self._client = self._client_factory(token=tokens.access_token, refresh_token=tokens.refresh_token)

    def _renew(self) -> None:
        stored = self._store.load_tokens()
        if stored is not None and stored != self._tokens:
            # Another process sharing the keychain already rotated the tokens.
            self._adopt(stored)
            return
        try:
            data = self._client.refresh_token()["data"]
            fresh = Tokens(data["access_token"], data["refresh_token"])
        except requests.RequestException as err:
            raise WyzeError(f"Could not reach Wyze ({type(err).__name__}).") from None
        except (WyzeClientError, KeyError, TypeError):
            raise NotLoggedIn(_SESSION_EXPIRED) from None
        self._store.save_tokens(fresh)
        self._adopt(fresh)

    def _attempt[T](self, operation: Callable[[Any], T]) -> T:
        try:
            return operation(self._client)
        except WyzeApiError as err:
            if _token_expired(err):
                raise _TokenExpired from None
            code = _error_data(err).get("code", "unknown")
            raise WyzeError(f"Wyze rejected the request (code {code}).") from None
        except WyzeClientError as err:
            raise WyzeError(f"The Wyze request failed ({type(err).__name__}).") from None
        except requests.RequestException as err:
            raise WyzeError(f"Could not reach Wyze ({type(err).__name__}).") from None

    def _call[T](self, operation: Callable[[Any], T]) -> T:
        with self._lock:
            if self._client is None:
                tokens = self._store.load_tokens()
                if tokens is None:
                    raise NotLoggedIn(_NOT_LOGGED_IN)
                self._adopt(tokens)
            try:
                return self._attempt(operation)
            except _TokenExpired:
                self._renew()
            try:
                return self._attempt(operation)
            except _TokenExpired:
                raise NotLoggedIn(_SESSION_EXPIRED) from None

    def cameras(self) -> list[Camera]:
        devices = self._call(lambda client: client.devices_list())
        return [
            Camera(
                mac=device.mac,
                name=device.nickname or device.mac,
                model=device.product.model or "unknown",
                online=bool(device.is_online),
            )
            for device in devices
            if device is not None and device.type == "Camera" and device.mac
        ]

    def events(
        self,
        *,
        since: datetime,
        until: datetime,
        macs: Sequence[str] = (),
        limit: int = _PAGE,
        oldest_first: bool = False,
    ) -> list[CameraEvent]:
        """Events in [since, until], newest first unless `oldest_first`."""
        found: dict[str, CameraEvent] = {}
        begin, end = since, until
        step = timedelta(milliseconds=1)
        while len(found) < limit and begin <= end:
            want = min(_PAGE, limit - len(found))
            page = self._call(
                lambda client: client.events.list(
                    device_ids=list(macs),
                    begin=begin,
                    end=end,
                    limit=want,
                    order_by=1 if oldest_first else 2,
                )
            )
            events = [event for event in map(_to_event, page) if event is not None]
            for event in events:
                found.setdefault(event.id, event)
            if len(page) < want or not events:
                break
            if oldest_first:
                begin = max(event.time for event in events) + step
            else:
                end = min(event.time for event in events) - step
        return sorted(found.values(), key=lambda event: event.time, reverse=not oldest_first)[:limit]

    def event_image(self, event: CameraEvent) -> tuple[bytes, str]:
        if event.image_url is None:
            raise ImageError("That event has no image.")
        return download_image(self._http, event.image_url, headers=_IMAGE_HEADERS)

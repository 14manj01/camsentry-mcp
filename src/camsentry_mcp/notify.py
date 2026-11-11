"""Tells the user what is going on: a text message, a phone push, or a webhook call.

Every channel is one HTTPS request, so nothing extra has to be installed. Destinations
(phone number, push topic, webhook URL) come from the OS keychain and never appear in
error messages or logs.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .config import ALERT_TYPES
from .credentials import SecretStore
from .policy import alert_key

log = logging.getLogger("camsentry")

SMS_ENDPOINT = "https://textbelt.com/text"
# Textbelt's shared key. It sends one free text a day in the countries where Textbelt still
# allows free texts; the United States is not one of them, so US numbers need a paid key.
FREE_SMS_KEY = "textbelt"
MAX_TEXT = 300

_TITLES = {
    "intruder": "Possible intruder",
    "package_taken": "Package may have been taken",
    "lingering": "Someone is lingering",
    "person": "Person seen",
    "package": "Package delivered",
    "rule": "Custom alert",
    "vehicle": "Vehicle seen",
    "pet": "Pet seen",
}
_URGENT = {"intruder", "package_taken"}


class NotifyError(Exception):
    """A notification could not be delivered. The message never contains a destination."""


@dataclass(frozen=True)
class Delivery:
    delivered: list[str]  # channels that accepted the message
    failures: list[str]  # one sentence per channel that did not


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    urgent: bool = False

    def text(self) -> str:
        """The single-string form used for SMS."""
        return f"{self.title}: {self.body}"[:MAX_TEXT]


def compose(report: dict[str, Any], wanted: frozenset[str]) -> Message | None:
    """The message for one analysed still, or None when nothing new and wanted happened in it."""
    fresh = set(report.get("new_alerts") or [])
    alerts = [alert for alert in report["alerts"] if alert_key(alert) in fresh and alert["type"] in wanted]
    if not alerts:
        return None
    top = min(alerts, key=lambda alert: ALERT_TYPES.index(alert["type"]))
    summary = (report.get("detection") or {}).get("summary", "")
    reason = top["reason"][:1].upper() + top["reason"][1:]
    if top["type"] in ("rule", "lingering", "package_taken"):
        detail = f"{reason}. {summary}".strip()
    else:
        detail = summary or reason
    when = datetime.fromisoformat(report["time"]).astimezone().strftime("%I:%M %p").lstrip("0")
    return Message(f"{_TITLES[top['type']]} at {report['camera']}", f"{detail} ({when})", top["type"] in _URGENT)


def normalize_phone(text: str) -> str:
    """Digits only, keeping a leading +. Raises ValueError for anything that cannot be a phone number."""
    digits = re.sub(r"\D", "", text)
    if not 10 <= len(digits) <= 15:
        raise ValueError("A phone number needs 10 to 15 digits.")
    return ("+" if text.strip().startswith("+") else "") + digits


def new_ntfy_topic() -> str:
    """ntfy has no accounts: an unguessable topic name is what keeps a feed private."""
    return "camsentry-" + secrets.token_urlsafe(18)


class Notifier:
    def __init__(self, secret_store: SecretStore, http: httpx.Client, ntfy_server: str) -> None:
        self._secrets = secret_store
        self._http = http
        self._ntfy_server = ntfy_server
        self._last_problem: str | None = None

    def channels(self) -> list[str]:
        """Names of the channels that have a destination configured."""
        saved = self._secrets.load_notify()
        return [name for name, key in (("sms", "sms_phone"), ("ntfy", "ntfy_topic"), ("webhook", "webhook_url")) if saved.get(key)]

    def _post(self, channel: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.post(url, timeout=20, **kwargs)
        except httpx.HTTPError as err:
            raise NotifyError(f"{channel}: could not reach the service ({type(err).__name__})") from None
        if response.status_code >= 400:
            raise NotifyError(f"{channel}: the service answered HTTP {response.status_code}")
        return response

    def _sms(self, saved: dict[str, str], message: Message) -> None:
        response = self._post(
            "sms", SMS_ENDPOINT, json={"phone": saved["sms_phone"], "message": message.text(), "key": saved.get("sms_key") or FREE_SMS_KEY}
        )
        try:
            result = response.json()
        except ValueError:
            raise NotifyError("sms: the service gave an unreadable answer") from None
        if not result.get("success"):
            reason = str(result.get("error") or "no reason given")[:120]
            raise NotifyError(f"sms: the service refused the text ({reason})")

    def _ntfy(self, saved: dict[str, str], message: Message) -> None:
        self._post(
            "ntfy",
            self._ntfy_server,
            json={
                "topic": saved["ntfy_topic"],
                "title": message.title,
                "message": message.body,
                "priority": 5 if message.urgent else 3,
                "tags": ["rotating_light"] if message.urgent else ["camera"],
            },
        )

    def _webhook(self, saved: dict[str, str], message: Message) -> None:
        self._post("webhook", saved["webhook_url"], json={"title": message.title, "body": message.body, "urgent": message.urgent})

    def send(self, message: Message) -> Delivery:
        """Deliver through every configured channel and report which worked and which did not.

        Raises NotifyError when nothing is configured or every channel failed.
        """
        saved = self._secrets.load_notify()
        senders = {"sms": self._sms, "ntfy": self._ntfy, "webhook": self._webhook}
        configured = self.channels()
        if not configured:
            raise NotifyError("No notification channel is set up. Run `camsentry-mcp setup` in a terminal.")
        delivered, failures = [], []
        for name in configured:
            try:
                senders[name](saved, message)
                delivered.append(name)
            except NotifyError as err:
                failures.append(str(err))
        if not delivered:
            raise NotifyError("; ".join(failures))
        problem = "; ".join(failures) or None
        if problem and problem != self._last_problem:
            log.warning("a notification channel is failing: %s", problem)
        self._last_problem = problem
        return Delivery(delivered, failures)

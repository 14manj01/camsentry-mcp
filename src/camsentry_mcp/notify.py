"""Tells the user what is going on: an email, a phone push, a text message, or a webhook call.

Nothing extra has to be installed: push, text and webhook are one HTTPS request each, and email
goes out through the user's own mailbox with Python's standard library. Destinations and the
mailbox password come from the OS keychain and never appear in error messages or logs.
"""

from __future__ import annotations

import logging
import re
import secrets
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
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

# Outgoing mail servers of the big mailbox providers, by address domain. All use STARTTLS on 587.
_SMTP_HOSTS = {
    "gmail.com": "smtp.gmail.com",
    "googlemail.com": "smtp.gmail.com",
    "outlook.com": "smtp-mail.outlook.com",
    "hotmail.com": "smtp-mail.outlook.com",
    "live.com": "smtp-mail.outlook.com",
    "yahoo.com": "smtp.mail.yahoo.com",
    "icloud.com": "smtp.mail.me.com",
    "me.com": "smtp.mail.me.com",
}
SMTP_PORT = 587

# Carrier addresses that turn an email into a text message, free of charge. They are disappearing:
# AT&T closed its gateway on 17 June 2025 and Verizon has announced the end of vtext.com by
# 31 March 2027 (https://www.verizon.com/support/vtext-vzwpix-shutdown). Carriers also filter
# these messages as spam without telling the sender, so a text can silently fail to arrive.
TEXT_GATEWAYS = {
    "verizon": "vtext.com",
    "t-mobile": "tmomail.net",
    "google-fi": "msg.fi.google.com",
}
MAX_SMS = 160

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


def smtp_host_for(address: str) -> str | None:
    """The outgoing mail server for a mailbox at one of the big providers, else None."""
    return _SMTP_HOSTS.get(address.rpartition("@")[2].lower())


def valid_email(address: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address) is not None


def text_address(phone: str, carrier: str) -> str:
    """The email address at a carrier that delivers to a US mobile number as a text message."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("A carrier text address needs a 10-digit US mobile number.")
    return f"{digits}@{TEXT_GATEWAYS[carrier]}"


class Notifier:
    def __init__(
        self,
        secret_store: SecretStore,
        http: httpx.Client,
        ntfy_server: str,
        smtp: Callable[..., Any] = smtplib.SMTP,
    ) -> None:
        self._secrets = secret_store
        self._http = http
        self._ntfy_server = ntfy_server
        self._smtp = smtp
        self._last_problem: str | None = None

    def channels(self) -> list[str]:
        """Names of the channels that have a destination configured."""
        saved = self._secrets.load_notify()
        known = (("email", "email_address"), ("sms", "sms_phone"), ("ntfy", "ntfy_topic"), ("webhook", "webhook_url"))
        return [name for name, key in known if saved.get(key)]

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

    def _email(self, saved: dict[str, str], message: Message) -> None:
        """Send through the user's own mailbox: it signs in as them and mails the alert."""
        mail = EmailMessage()
        mail["From"] = saved["email_address"]
        mail["To"] = recipient = saved.get("email_to") or saved["email_address"]
        if recipient.rpartition("@")[2].lower() in TEXT_GATEWAYS.values():
            # It will be read as a text message: one short line, no subject for the carrier to prepend.
            text = message.text()
            mail.set_content(text if len(text) <= MAX_SMS else text[: MAX_SMS - 3].rstrip() + "...")
        else:
            mail["Subject"] = ("URGENT: " if message.urgent else "") + " ".join(message.title.split())
            mail.set_content(message.body)
        refused = NotifyError("email: the mail server refused the sign-in. Check the address and the app password.")
        try:
            with self._smtp(saved["email_host"], SMTP_PORT, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                try:
                    server.login(saved["email_address"], saved["email_password"])
                except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected):
                    # Seen with Gmail: for an account it does not know, it hangs up instead of answering.
                    raise refused from None
                server.send_message(mail)
        except (smtplib.SMTPException, OSError) as err:
            raise NotifyError(f"email: could not send ({type(err).__name__})") from None

    def send(self, message: Message) -> Delivery:
        """Deliver through every configured channel and report which worked and which did not.

        Raises NotifyError when nothing is configured or every channel failed.
        """
        saved = self._secrets.load_notify()
        senders = {"email": self._email, "sms": self._sms, "ntfy": self._ntfy, "webhook": self._webhook}
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

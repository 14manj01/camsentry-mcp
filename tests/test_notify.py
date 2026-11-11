from __future__ import annotations

import smtplib

import httpx
import pytest

from conftest import Outbox, at, seen
from camsentry_mcp.config import ALERT_TYPES, DEFAULT_NOTIFY
from camsentry_mcp.credentials import SecretStore
from camsentry_mcp.notify import (
    Message,
    Notifier,
    NotifyError,
    compose,
    new_ntfy_topic,
    normalize_phone,
    smtp_host_for,
    valid_email,
)

PHONE = "+15550100123"


def report(alerts: list[tuple[str, str]], new: list[str], summary: str = "A person stands at the door.") -> dict:
    return {
        "camera": "Front Door",
        "time": at("2026-03-01 23:41").isoformat(timespec="seconds"),
        "detection": seen(summary=summary).to_dict(),
        "alerts": [{"type": kind, "reason": reason, "priority": "normal", **({"rule_id": 7} if kind == "rule" else {})} for kind, reason in alerts],
        "new_alerts": new,
    }


def test_message_leads_with_the_most_important_new_alert() -> None:
    message = compose(report([("intruder", "person detected while armed"), ("person", "1 visible")], ["intruder", "person"]), DEFAULT_NOTIFY)
    assert message == Message("Possible intruder at Front Door", "A person stands at the door. (11:41 PM)", urgent=True)
    assert message.text() == "Possible intruder at Front Door: A person stands at the door. (11:41 PM)"


def test_nothing_is_sent_for_alerts_that_were_already_announced() -> None:
    assert compose(report([("person", "1 visible")], []), DEFAULT_NOTIFY) is None


def test_quiet_alert_types_are_not_sent_unless_asked_for() -> None:
    pet = report([("pet", "dog")], ["pet"], "A dog crosses the lawn.")
    assert compose(pet, DEFAULT_NOTIFY) is None
    assert compose(pet, frozenset(ALERT_TYPES)).title == "Pet seen at Front Door"


def test_an_unwanted_alert_does_not_hide_a_wanted_one() -> None:
    message = compose(report([("intruder", "x"), ("package", "1 visible")], ["intruder", "package"], "A box on the step."), frozenset({"package"}))
    assert (message.title, message.urgent) == ("Package delivered at Front Door", False)


def test_rule_and_lingering_messages_say_what_triggered_them() -> None:
    rule = compose(report([("rule", "the gate is open")], ["rule:7"], "A wooden gate stands ajar."), DEFAULT_NOTIFY)
    assert rule.body == "The gate is open. A wooden gate stands ajar. (11:41 PM)"
    lingering = compose(report([("lingering", "a person has been in view for about 6 minutes")], ["lingering"]), DEFAULT_NOTIFY)
    assert lingering.title == "Someone is lingering at Front Door"
    assert lingering.body.startswith("A person has been in view for about 6 minutes. ")


def test_sms_text_is_capped() -> None:
    assert len(Message("Title", "x" * 1000).text()) == 300


@pytest.mark.parametrize(("typed", "stored"), [("(555) 010-0123", "5550100123"), ("+1 555-010-0123", "+15550100123"), ("555.010.0123", "5550100123")])
def test_phone_numbers_are_normalised(typed: str, stored: str) -> None:
    assert normalize_phone(typed) == stored


@pytest.mark.parametrize("typed", ["", "12345", "call me", "1" * 16])
def test_things_that_are_not_phone_numbers_are_rejected(typed: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(typed)


def test_ntfy_topics_are_unguessable_and_unique() -> None:
    first, second = new_ntfy_topic(), new_ntfy_topic()
    assert first != second and len(first) >= 30


def notifier(store: SecretStore, outbox: Outbox) -> Notifier:
    return Notifier(store, httpx.Client(transport=httpx.MockTransport(outbox)), "https://ntfy.example")


URGENT = Message("Possible intruder at Front Door", "A person stands at the door. (11:41 PM)", urgent=True)


def test_sms_goes_to_the_stored_number_with_the_stored_key(store: SecretStore) -> None:
    outbox = Outbox()
    store.save_notify({"sms_phone": PHONE, "sms_key": "paid-key"})
    assert notifier(store, outbox).send(URGENT).delivered == ["sms"]
    assert outbox.bodies("textbelt.com") == [{"phone": PHONE, "message": URGENT.text(), "key": "paid-key"}]


def test_ntfy_push_carries_title_body_and_urgency(store: SecretStore) -> None:
    outbox = Outbox()
    store.save_notify({"ntfy_topic": "camsentry-secret-topic"})
    send = notifier(store, outbox).send
    send(URGENT)
    send(Message("Package delivered at Front Door", "A box on the step. (10:00 AM)"))
    urgent, routine = outbox.bodies("ntfy.example")
    assert (urgent["topic"], urgent["title"], urgent["message"], urgent["priority"]) == ("camsentry-secret-topic", URGENT.title, URGENT.body, 5)
    assert routine["priority"] == 3


def test_webhook_receives_the_alert_as_json(store: SecretStore) -> None:
    outbox = Outbox()
    store.save_notify({"webhook_url": "https://hooks.example/abc"})
    notifier(store, outbox).send(URGENT)
    assert outbox.bodies("hooks.example") == [{"title": URGENT.title, "body": URGENT.body, "urgent": True}]


def test_one_failing_channel_does_not_stop_the_others(store: SecretStore) -> None:
    outbox = Outbox()
    outbox.fail.add("textbelt.com")
    store.save_notify({"sms_phone": PHONE, "ntfy_topic": "camsentry-secret-topic"})
    delivery = notifier(store, outbox).send(URGENT)
    assert delivery.delivered == ["ntfy"]
    assert delivery.failures == ["sms: the service answered HTTP 503"]


def test_refused_text_is_an_error_that_does_not_reveal_the_number(store: SecretStore) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": "Out of quota"})

    store.save_notify({"sms_phone": PHONE})
    with pytest.raises(NotifyError) as caught:
        Notifier(store, httpx.Client(transport=httpx.MockTransport(refuse)), "https://ntfy.example").send(URGENT)
    assert "Out of quota" in str(caught.value)
    assert "5550100123" not in str(caught.value)


def test_unreachable_services_do_not_leak_the_push_topic_or_webhook(store: SecretStore) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    store.save_notify({"ntfy_topic": "camsentry-secret-topic", "webhook_url": "https://hooks.example/private-path"})
    with pytest.raises(NotifyError) as caught:
        Notifier(store, httpx.Client(transport=httpx.MockTransport(down)), "https://ntfy.example").send(URGENT)
    assert "secret-topic" not in str(caught.value) and "private-path" not in str(caught.value)


def test_sending_with_nothing_set_up_says_how_to_fix_it(store: SecretStore) -> None:
    with pytest.raises(NotifyError, match="camsentry-mcp setup"):
        notifier(store, Outbox()).send(URGENT)


# -- email through the user's own mailbox --------------------------------------

MAILBOX = {"email_address": "me@gmail.com", "email_password": "app-pass-word", "email_host": "smtp.gmail.com", "email_to": "me@gmail.com"}


class FakeMailServer:
    """Stands in for smtplib.SMTP and records what a mail client did with it."""

    def __init__(self, reject_login: bool = False, unreachable: bool = False) -> None:
        self.reject_login, self.unreachable = reject_login, unreachable
        self.connected_to: tuple[str, int] | None = None
        self.encrypted = False
        self.logins: list[tuple[str, str]] = []
        self.sent: list = []

    def __call__(self, host: str, port: int, timeout: float):
        if self.unreachable:
            raise OSError("network is unreachable")
        self.connected_to = (host, port)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def starttls(self, context) -> None:
        self.encrypted = True

    def login(self, user: str, password: str) -> None:
        if not self.encrypted:
            raise AssertionError("password sent before the connection was encrypted")
        if self.reject_login:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted for app-pass-word")
        self.logins.append((user, password))

    def send_message(self, mail) -> None:
        self.sent.append(mail)


def mailer(store: SecretStore, server: FakeMailServer) -> Notifier:
    return Notifier(store, httpx.Client(transport=httpx.MockTransport(Outbox())), "https://ntfy.example", smtp=server)


def test_email_is_sent_from_the_users_mailbox_over_an_encrypted_connection(store: SecretStore) -> None:
    server = FakeMailServer()
    store.save_notify(MAILBOX)

    assert mailer(store, server).send(URGENT).delivered == ["email"]

    assert server.connected_to == ("smtp.gmail.com", 587)
    assert server.logins == [("me@gmail.com", "app-pass-word")]
    (mail,) = server.sent
    assert (mail["From"], mail["To"]) == ("me@gmail.com", "me@gmail.com")
    assert mail["Subject"] == "URGENT: Possible intruder at Front Door"
    assert mail.get_content().strip() == URGENT.body


def test_email_can_go_to_a_different_address_and_routine_alerts_are_not_marked_urgent(store: SecretStore) -> None:
    server = FakeMailServer()
    store.save_notify({**MAILBOX, "email_to": "5550100123@carrier.example"})
    mailer(store, server).send(Message("Package delivered at Front Door", "A box on the step. (10:00 AM)"))
    (mail,) = server.sent
    assert (mail["To"], mail["Subject"]) == ("5550100123@carrier.example", "Package delivered at Front Door")


def test_a_camera_name_with_a_line_break_cannot_inject_mail_headers(store: SecretStore) -> None:
    server = FakeMailServer()
    store.save_notify(MAILBOX)
    mailer(store, server).send(Message("Person seen at Door\r\nBcc: stranger@example.com", "body"))
    (mail,) = server.sent
    assert mail["Bcc"] is None
    assert mail["Subject"] == "Person seen at Door Bcc: stranger@example.com"


def test_rejected_mail_sign_in_points_at_the_app_password_without_revealing_it(store: SecretStore) -> None:
    store.save_notify(MAILBOX)
    with pytest.raises(NotifyError, match="app password") as caught:
        mailer(store, FakeMailServer(reject_login=True)).send(URGENT)
    assert "app-pass-word" not in str(caught.value)


def test_unreachable_mail_server_does_not_stop_the_other_channels(store: SecretStore) -> None:
    store.save_notify({**MAILBOX, "ntfy_topic": "camsentry-secret-topic"})
    delivery = mailer(store, FakeMailServer(unreachable=True)).send(URGENT)
    assert delivery.delivered == ["ntfy"]
    assert delivery.failures == ["email: could not send (OSError)"]


@pytest.mark.parametrize(("address", "host"), [("me@gmail.com", "smtp.gmail.com"), ("Me@Outlook.com", "smtp-mail.outlook.com"), ("me@icloud.com", "smtp.mail.me.com"), ("me@mycompany.example", None)])
def test_big_mail_providers_need_no_server_name(address: str, host: str | None) -> None:
    assert smtp_host_for(address) == host


@pytest.mark.parametrize(("text", "ok"), [("me@gmail.com", True), ("5550100123@carrier.example", True), ("me@gmail", False), ("not an address", False), ("a@b@c.com", False), ("", False)])
def test_email_addresses_are_checked(text: str, ok: bool) -> None:
    assert valid_email(text) is ok

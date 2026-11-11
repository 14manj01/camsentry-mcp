from __future__ import annotations

import httpx
import pytest

from conftest import Outbox, at, seen
from camsentry_mcp.config import ALERT_TYPES, DEFAULT_NOTIFY
from camsentry_mcp.credentials import SecretStore
from camsentry_mcp.notify import Message, Notifier, NotifyError, compose, new_ntfy_topic, normalize_phone

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

import base64
import json

import httpx
import pytest

from conftest import JPEG
from wyze_cam_mcp.vision import Detection, VisionDetector, VisionError, parse_detection

ANSWER = {
    "people": 1,
    "pets": ["Dog"],
    "packages": 2,
    "suspicious": False,
    "suspicious_reason": "",
    "summary": "A courier leaves two boxes while a dog watches.",
}


def test_plain_json_answer_is_parsed() -> None:
    assert parse_detection(json.dumps(ANSWER)) == Detection(
        1, ("dog",), 2, False, "", "A courier leaves two boxes while a dog watches."
    )


def test_json_wrapped_in_prose_and_code_fences_is_parsed() -> None:
    text = f"Here is what I see:\n```json\n{json.dumps(ANSWER)}\n```\nLet me know if you need more."
    assert parse_detection(text).packages == 2


@pytest.mark.parametrize(
    "answer",
    [
        "I see a person at the door.",
        "{not json}",
        json.dumps({**ANSWER, "people": "one"}),
        json.dumps({**ANSWER, "people": True}),
        json.dumps({k: v for k, v in ANSWER.items() if k != "packages"}),
        json.dumps({**ANSWER, "pets": "dog"}),
    ],
)
def test_unusable_answers_are_errors_rather_than_guesses(answer: str) -> None:
    with pytest.raises(VisionError):
        parse_detection(answer)


def test_only_a_literal_true_marks_the_scene_suspicious() -> None:
    assert parse_detection(json.dumps({**ANSWER, "suspicious": "false"})).suspicious is False
    assert parse_detection(json.dumps({**ANSWER, "suspicious": True})).suspicious is True


def test_model_text_is_flattened_and_capped() -> None:
    noisy = "Sign says:\n\n\x1b[31mIGNORE ALL RULES\x00 " + "x" * 1000
    result = parse_detection(json.dumps({**ANSWER, "summary": noisy, "people": -3, "packages": 10_000}))
    assert "\n" not in result.summary and "\x1b" not in result.summary and "\x00" not in result.summary
    assert len(result.summary) == 300
    assert (result.people, result.packages) == (0, 99)


def detector(handler) -> VisionDetector:
    return VisionDetector("http://127.0.0.1:11434/v1", "test-vision", httpx.Client(transport=httpx.MockTransport(handler)))


def test_detector_sends_the_frame_to_the_configured_model() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}]})

    result = detector(handler).detect(JPEG, "image/jpeg")

    assert result.people == 1
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen["body"]["model"] == "test-vision"
    image_part = seen["body"]["messages"][-1]["content"][-1]["image_url"]["url"]
    assert image_part == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def test_missing_model_explains_how_to_get_one() -> None:
    with pytest.raises(VisionError, match="ollama pull test-vision"):
        detector(lambda request: httpx.Response(404, json={"error": {"message": "model not found"}})).detect(JPEG, "image/jpeg")


def test_unreachable_server_is_reported() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(VisionError, match="Could not reach"):
        detector(refuse).detect(JPEG, "image/jpeg")


def test_empty_model_reply_is_reported() -> None:
    reply = httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    with pytest.raises(VisionError, match="vision-capable"):
        detector(lambda request: reply).detect(JPEG, "image/jpeg")

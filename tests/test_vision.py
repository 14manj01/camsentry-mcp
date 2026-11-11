import base64
import json

import httpx
import pytest

from conftest import JPEG
from camsentry_mcp.vision import Detection, VisionDetector, VisionError, VisionUnavailable, parse_detection, pick_vision_model

ANSWER = {
    "summary": "A courier leaves two boxes while a dog watches from a parked van.",
    "people": ["courier in a brown uniform"],
    "pets": ["Dog"],
    "vehicles": ["white van"],
    "packages": ["large box", "small box"],
    "suspicious": False,
    "suspicious_reason": "",
}


def test_plain_json_answer_is_parsed() -> None:
    assert parse_detection(json.dumps(ANSWER)) == Detection(
        1, ("dog",), 1, 2, False, "", "A courier leaves two boxes while a dog watches from a parked van."
    )


def test_json_wrapped_in_prose_and_code_fences_is_parsed() -> None:
    text = f"Here is what I see:\n```json\n{json.dumps(ANSWER)}\n```\nLet me know if you need more."
    assert parse_detection(text).packages == 2


@pytest.mark.parametrize(
    "answer",
    [
        "I see a person at the door.",
        "{not json}",
        json.dumps({**ANSWER, "people": 1}),
        json.dumps({**ANSWER, "people": "one"}),
        json.dumps({k: v for k, v in ANSWER.items() if k != "packages"}),
        json.dumps({k: v for k, v in ANSWER.items() if k != "vehicles"}),
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
    result = parse_detection(json.dumps({**ANSWER, "summary": noisy, "packages": ["box"] * 500, "pets": ["cat"] * 50}))
    assert "\n" not in result.summary and "\x1b" not in result.summary and "\x00" not in result.summary
    assert len(result.summary) == 300
    assert (result.packages, len(result.pets)) == (99, 10)


def test_blank_entries_and_words_meaning_nothing_are_not_counted() -> None:
    answer = {**ANSWER, "people": ["", "  ", None, "man at the door", "None"], "vehicles": ["n/a"], "pets": ["nothing"]}
    result = parse_detection(json.dumps(answer))
    assert (result.people, result.vehicles, result.pets) == (1, 0, ())


def detector(handler, model: str | None = "test-vision") -> VisionDetector:
    return VisionDetector("http://127.0.0.1:11434/v1", model, httpx.Client(transport=httpx.MockTransport(handler)))


def evidence(*entries: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"evidence": list(entries)})}}]})


def camera_model(answer: dict, humans: tuple[str, ...] = (), parcels: tuple[str, ...] = ()):
    """A fake vision model: the first request describes the frame with `answer`.

    Later requests are second looks: asked only for human beings it lists `humans`, asked only for
    parcels it lists `parcels`.
    """
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((str(request.url), body))
        prompt = body["messages"][-1]["content"][0]["text"]
        if len(requests) == 1:
            reply = answer
        elif "human beings" in prompt:
            reply = {"people": list(humans)}
        else:
            reply = {"packages": list(parcels)}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    return handler, requests


def test_detector_sends_the_frame_to_the_configured_model() -> None:
    handler, requests = camera_model(ANSWER, humans=("courier at the door on the right",), parcels=("box on the step", "box by the door"))

    result = detector(handler).detect(JPEG, "image/jpeg")

    assert (result.people, result.vehicles, result.packages) == (1, 1, 2)
    url, body = requests[0]
    assert url == "http://127.0.0.1:11434/v1/chat/completions"
    assert body["model"] == "test-vision"
    assert body["messages"][-1]["content"][-1]["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def test_a_person_is_reported_only_when_a_second_look_also_finds_one() -> None:
    phantom_person, _ = camera_model(ANSWER, parcels=("box on the step",))
    result = detector(phantom_person).detect(JPEG, "image/jpeg")
    assert result.people == 0, "the second look found no person"
    assert (result.pets, result.vehicles, result.packages) == (("dog",), 1, 2), "everything else is kept"


def test_a_package_is_reported_only_when_a_second_look_also_finds_one() -> None:
    phantom_package, _ = camera_model(ANSWER, humans=("courier at the door",))
    result = detector(phantom_package).detect(JPEG, "image/jpeg")
    assert (result.people, result.packages) == (1, 0)


def test_frames_without_people_or_packages_cost_a_single_model_call() -> None:
    handler, requests = camera_model({**ANSWER, "people": [], "packages": []})
    result = detector(handler).detect(JPEG, "image/jpeg")
    assert (result.people, result.packages, result.vehicles) == (0, 0, 1)
    assert len(requests) == 1


def test_each_custom_statement_is_judged_on_its_own_by_the_evidence_for_it() -> None:
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"][0]["text"]
        asked.append(prompt)
        return evidence("wooden gate standing open") if "gate" in prompt else evidence()

    verdicts = detector(handler).check(JPEG, "image/jpeg", ["a car is in the driveway", "the gate is open"])

    assert verdicts == (False, True)
    assert len(asked) == 2 and "Statement: a car is in the driveway" in asked[0] and "gate" not in asked[0]


def test_evidence_that_says_nothing_was_seen_is_not_evidence() -> None:
    assert detector(lambda request: evidence("none", " ")).check(JPEG, "image/jpeg", ["the gate is open"]) == (False,)


def test_no_statements_means_no_model_calls() -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the model must not be called")

    assert detector(unexpected).check(JPEG, "image/jpeg", []) == ()


def ollama(models: dict[str, tuple[int, list[str]]]):
    """A fake Ollama server: name -> (size in bytes, capabilities)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": name, "size": size} for name, (size, _) in models.items()]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": models[json.loads(request.content)["model"]][1]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}]})

    return handler


def test_without_a_tested_model_the_smallest_installed_vision_model_is_chosen() -> None:
    server = ollama({
        "big-vision:27b": (17_000_000_000, ["completion", "vision", "tools"]),
        "tiny-text:1b": (800_000_000, ["completion", "tools"]),
        "small-vision:4b": (3_400_000_000, ["completion", "vision"]),
    })
    client = httpx.Client(transport=httpx.MockTransport(server))
    assert pick_vision_model(client, "http://127.0.0.1:11434/v1") == "small-vision:4b"
    assert detector(server, model=None).model() == "small-vision:4b"


def test_a_model_that_did_well_on_real_camera_stills_wins_over_a_smaller_one() -> None:
    server = ollama({
        "tiny-vision:2b": (1_900_000_000, ["completion", "vision"]),
        "gemma3:4b": (3_400_000_000, ["completion", "vision"]),
        "qwen2.5vl:3b": (3_200_000_000, ["completion", "vision"]),
    })
    assert detector(server, model=None).model() == "qwen2.5vl:3b"


def test_no_vision_model_installed_says_how_to_get_one() -> None:
    server = ollama({"tiny-text:1b": (800_000_000, ["completion"])})
    with pytest.raises(VisionUnavailable, match="ollama pull qwen2.5vl:3b"):
        detector(server, model=None).detect(JPEG, "image/jpeg")


def test_a_server_that_is_not_ollama_asks_for_the_model_name() -> None:
    with pytest.raises(VisionUnavailable, match="CAMSENTRY_VISION_MODEL"):
        detector(lambda request: httpx.Response(200, json={"data": []}), model=None).model()


def test_missing_model_explains_how_to_get_one() -> None:
    with pytest.raises(VisionUnavailable, match="ollama pull test-vision"):
        detector(lambda request: httpx.Response(404, json={"error": {"message": "model not found"}})).detect(JPEG, "image/jpeg")


def test_unreachable_server_is_reported_as_temporarily_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(VisionUnavailable, match="Could not reach"):
        detector(refuse).detect(JPEG, "image/jpeg")


def test_empty_model_reply_is_a_problem_with_that_frame_only() -> None:
    reply = httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    with pytest.raises(VisionError, match="vision-capable") as caught:
        detector(lambda request: reply).detect(JPEG, "image/jpeg")
    assert not isinstance(caught.value, VisionUnavailable)

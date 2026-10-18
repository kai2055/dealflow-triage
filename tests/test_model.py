"""Tests for the model client: settings, the exact request sent, the call record, and every named failure.

No real model is called: a fake server answers inside the test (no network, no cost).
"""

import json

import httpx2
import pytest

from triage.model import (ModelCallError, ModelSettings, OpenAICompatibleClient, SettingsError,
                          load_settings)
from triage.pages import Page

SCHEMA = {"type": "object", "properties": {"company_name": {"type": "string"}},
          "required": ["company_name"], "additionalProperties": False}
OK_BODY = {
    "id": "gen-1", "object": "chat.completion", "created": 1, "model": "anthropic/claude-sonnet-5.5",
    "provider": "Anthropic",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": '{"company_name": "Cyclica"}'}}],
    "usage": {"prompt_tokens": 1900, "completion_tokens": 40, "total_tokens": 1940, "cost": 0.0042},
}
TRIAGE_VARS = ["TRIAGE_BASE_URL", "TRIAGE_MODEL", "TRIAGE_API_KEY", "TRIAGE_OUTPUT_MODE",
               "TRIAGE_TEMPERATURE", "TRIAGE_DATA_COLLECTION", "TRIAGE_TIMEOUT_SECONDS", "TRIAGE_MAX_RETRIES"]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in TRIAGE_VARS:  # settings on this computer must never leak into a test
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def pages(tmp_path):
    picture = tmp_path / "page-001.jpg"
    picture.write_bytes(b"\xff\xd8 fake picture")
    return [Page(1, "Seed round CAD 20M", picture), Page(2, "", picture)]


def settings(**changes):
    base = dict(base_url="https://openrouter.example/api/v1", model="anthropic/claude-sonnet-5.5",
                api_key="test-key", max_retries=1)
    return ModelSettings(**{**base, **changes})


def fake_server(*responses):
    """A client whose server replies with `responses` in order and remembers every request."""
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        status, body = responses[min(len(seen), len(responses)) - 1]
        return httpx2.Response(status, json=body, headers={"retry-after-ms": "1"})

    return httpx2.Client(transport=httpx2.MockTransport(handle)), seen


def test_settings_come_from_the_env_file_and_the_terminal_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_BASE_URL=https://openrouter.ai/api/v1\nTRIAGE_MODEL=from-file\nTRIAGE_API_KEY=k\n")
    monkeypatch.setenv("TRIAGE_MODEL", "from-terminal")
    s = load_settings(env)
    assert s.model == "from-terminal"
    assert (s.output_mode, s.temperature, s.data_collection) == ("strict", 0.0, "deny")
    assert (s.timeout_seconds, s.max_retries) == (180.0, 2)


def test_missing_key_is_named_clearly(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_BASE_URL=https://openrouter.ai/api/v1\nTRIAGE_MODEL=m\n")
    with pytest.raises(SettingsError, match="TRIAGE_API_KEY"):
        load_settings(env)


def test_unknown_output_mode_is_refused(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TRIAGE_BASE_URL=u\nTRIAGE_MODEL=m\nTRIAGE_API_KEY=k\nTRIAGE_OUTPUT_MODE=loose\n")
    with pytest.raises(SettingsError, match="TRIAGE_OUTPUT_MODE"):
        load_settings(env)


def test_strict_request_carries_schema_privacy_and_pages_in_order(pages):
    http, seen = fake_server((200, OK_BODY))
    OpenAICompatibleClient(settings(), http).read("Read the deck.", pages, "pitch_card", SCHEMA)
    body = seen[0]
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["temperature"] == 0.0
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"] == {"data_collection": "deny", "require_parameters": True}
    parts = body["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "text", "image_url"]
    assert parts[0]["text"].startswith("--- Page 1 ---")
    assert "no text layer" in parts[2]["text"]  # image-only page still gets its number
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_json_mode_puts_the_schema_in_the_instructions(pages):
    http, seen = fake_server((200, OK_BODY))
    OpenAICompatibleClient(settings(output_mode="json"), http).read("Read the deck.", pages, "pitch_card", SCHEMA)
    body = seen[0]
    assert body["response_format"] == {"type": "json_object"}
    assert '"company_name"' in body["messages"][0]["content"]
    assert body["provider"] == {"data_collection": "deny"}


def test_temperature_none_is_not_sent(pages):
    http, seen = fake_server((200, OK_BODY))
    OpenAICompatibleClient(settings(temperature=None), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert "temperature" not in seen[0]


def test_answer_and_call_record_come_back(pages):
    http, _ = fake_server((200, OK_BODY))
    result = OpenAICompatibleClient(settings(), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert json.loads(result.raw_text) == {"company_name": "Cyclica"}
    r = result.record
    assert (r.requested_model, r.answered_model, r.provider) == (
        "anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-5.5", "Anthropic")
    assert (r.input_tokens, r.output_tokens, r.cost) == (1900, 40, 0.0042)
    assert r.called_at.endswith("+00:00")


def test_bad_key_stops_at_once_without_retrying(pages):
    http, seen = fake_server((401, {"error": {"message": "No auth"}}))
    with pytest.raises(ModelCallError) as caught:
        OpenAICompatibleClient(settings(), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert caught.value.kind == "bad_key"
    assert len(seen) == 1


def test_server_error_is_retried_then_succeeds(pages):
    http, seen = fake_server((500, {"error": {"message": "busy"}}), (200, OK_BODY))
    result = OpenAICompatibleClient(settings(max_retries=1), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert len(seen) == 2
    assert result.record.provider == "Anthropic"


def test_server_error_that_never_clears_is_unavailable(pages):
    http, seen = fake_server((503, {"error": {"message": "down"}}))
    with pytest.raises(ModelCallError) as caught:
        OpenAICompatibleClient(settings(max_retries=1), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert caught.value.kind == "unavailable"
    assert len(seen) == 2  # first try + 1 retry


def test_unsupported_request_is_rejected(pages):
    http, _ = fake_server((400, {"error": {"message": "response_format not supported"}}))
    with pytest.raises(ModelCallError) as caught:
        OpenAICompatibleClient(settings(), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert caught.value.kind == "rejected"


def test_empty_answer_is_named(pages):
    empty = {**OK_BODY, "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": ""}}]}
    http, _ = fake_server((200, empty))
    with pytest.raises(ModelCallError) as caught:
        OpenAICompatibleClient(settings(), http).read("Read.", pages, "pitch_card", SCHEMA)
    assert caught.value.kind == "empty_answer"
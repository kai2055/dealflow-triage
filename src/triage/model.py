"""The model client: sends a deck's pages to a model and brings back its raw answer, with a record of the call.

The rest of the code only uses `ModelClient` (the socket). `OpenAICompatibleClient` is one plug for it:
OpenRouter today, local Ollama by changing three settings (decision 34).
"""

import base64
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence

import openai
from dotenv import dotenv_values

from triage.pages import Page

OUTPUT_MODES = ("strict", "json")
DATA_COLLECTION = ("deny", "allow")


class SettingsError(Exception):
    """The model settings are missing or wrong. The message says which and where to look."""


class ModelCallError(Exception):
    """A model call that failed. `kind` is one of KINDS."""

    KINDS = ("bad_key", "rejected", "unavailable", "empty_answer")

    def __init__(self, kind: str, detail: str):
        if kind not in self.KINDS:
            raise ValueError(f"Unknown kind: {kind!r}")
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class ModelSettings:
    base_url: str
    model: str
    api_key: str
    output_mode: str = "strict"  # decision 53
    temperature: float | None = 0.0  # None means "don't send one" (decision 56)
    data_collection: str = "deny"  # decision 57
    timeout_seconds: float = 180.0  # decision 54
    max_retries: int = 2  # decision 54


def load_settings(env_file: Path = Path(".env")) -> ModelSettings:
    """Read the settings from .env; a variable set in the terminal wins over the file (decision 52)."""
    values = {**dotenv_values(env_file), **os.environ}

    def get(name: str, default: str | None = None) -> str | None:
        value = values.get(name)
        return value.strip() if value and value.strip() else default

    missing = [n for n in ("TRIAGE_BASE_URL", "TRIAGE_MODEL", "TRIAGE_API_KEY") if not get(n)]
    if missing:
        raise SettingsError(f"{', '.join(missing)} not set. See .env.example.")

    output_mode = get("TRIAGE_OUTPUT_MODE", "strict").lower()
    if output_mode not in OUTPUT_MODES:
        raise SettingsError(f"TRIAGE_OUTPUT_MODE must be strict or json, not {output_mode!r}")
    data_collection = get("TRIAGE_DATA_COLLECTION", "deny").lower()
    if data_collection not in DATA_COLLECTION:
        raise SettingsError(f"TRIAGE_DATA_COLLECTION must be deny or allow, not {data_collection!r}")

    temperature_text = get("TRIAGE_TEMPERATURE", "0").lower()
    try:
        temperature = None if temperature_text == "none" else float(temperature_text)
        timeout = float(get("TRIAGE_TIMEOUT_SECONDS", "180"))
        retries = int(get("TRIAGE_MAX_RETRIES", "2"))
    except ValueError as e:
        raise SettingsError(f"A number setting is not a number: {e}") from e

    return ModelSettings(get("TRIAGE_BASE_URL"), get("TRIAGE_MODEL"), get("TRIAGE_API_KEY"),
                         output_mode, temperature, data_collection, timeout, retries)


@dataclass(frozen=True)
class CallRecord:
    """Who answered, with what settings, and what it cost. Saved next to the card (decision 44)."""

    requested_model: str
    answered_model: str | None
    provider: str | None
    called_at: str  # UTC, ISO format
    output_mode: str
    temperature: float | None
    data_collection: str
    input_tokens: int | None
    output_tokens: int | None
    cost: float | None  # as reported by OpenRouter; None if not reported


@dataclass(frozen=True)
class ModelResult:
    raw_text: str  # always kept, even if it later fails the card check (decision 55)
    record: CallRecord


class ModelClient(Protocol):
    """The socket: anything that can read pages and answer in the given shape."""

    def read(self, instructions: str, pages: Sequence[Page], schema_name: str, schema: dict) -> ModelResult:
        ...


def _page_content(page: Page) -> list[dict]:
    """One page as the model sees it: its number and text, then its picture."""
    text = page.text.strip() if page.has_text else "(no text layer; read the picture)"
    picture = base64.b64encode(page.image_path.read_bytes()).decode("ascii")
    return [
        {"type": "text", "text": f"--- Page {page.number} ---\n{text}"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{picture}"}},
    ]


class OpenAICompatibleClient:
    """The plug: any server that speaks the OpenAI chat format (OpenRouter, Ollama)."""

    def __init__(self, settings: ModelSettings, http_client=None):
        self.settings = settings
        self._client = openai.OpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,  # temporary problems only; the library never retries a bad key
            http_client=http_client,
        )

    def _request(self, instructions: str, pages: Sequence[Page], schema_name: str, schema: dict) -> dict:
        s = self.settings
        provider = {"data_collection": s.data_collection}
        if s.output_mode == "strict":
            response_format = {"type": "json_schema",
                               "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
            provider["require_parameters"] = True
        else:
            response_format = {"type": "json_object"}
            instructions += "\n\nAnswer with one JSON object that matches this schema:\n" + json.dumps(schema)

        content = [part for page in pages for part in _page_content(page)]
        request = {
            "model": s.model,
            "messages": [{"role": "system", "content": instructions},
                         {"role": "user", "content": content}],
            "response_format": response_format,
            "extra_body": {"provider": provider},  # OpenRouter's routing and privacy options
        }
        if s.temperature is not None:
            request["temperature"] = s.temperature
        return request

    def read(self, instructions: str, pages: Sequence[Page], schema_name: str, schema: dict) -> ModelResult:
        s = self.settings
        called_at = datetime.now(timezone.utc).isoformat()
        try:
            response = self._client.chat.completions.create(**self._request(instructions, pages, schema_name, schema))
        except openai.AuthenticationError as e:
            raise ModelCallError("bad_key", "the API key was refused") from e
        except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                openai.InternalServerError) as e:
            raise ModelCallError("unavailable", f"still failing after {s.max_retries} retries: {e}") from e
        except openai.APIStatusError as e:
            raise ModelCallError("rejected", f"HTTP {e.status_code}: {e.message}") from e

        choice = response.choices[0] if response.choices else None
        raw_text = choice.message.content if choice else None
        if not raw_text or not raw_text.strip():
            raise ModelCallError("empty_answer", "the model returned no text")

        usage = response.usage
        extra = response.model_extra or {}
        usage_extra = (usage.model_extra or {}) if usage else {}
        record = CallRecord(
            requested_model=s.model,
            answered_model=response.model,
            provider=extra.get("provider"),
            called_at=called_at,
            output_mode=s.output_mode,
            temperature=s.temperature,
            data_collection=s.data_collection,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            cost=usage_extra.get("cost"),
        )
        return ModelResult(raw_text, record)
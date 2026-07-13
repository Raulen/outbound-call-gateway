"""Ultravox call-creation REST contract, mocked at the httpx transport
layer (respx) — no network, but the real request/response path runs."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from lk_ultravox_bridge.ultravox_client import (
    UltravoxCallClient,
    _VOICEMAIL_GUARD_PROMPT,
    _VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY,
)

from tests.conftest import make_config

log = logging.getLogger("test")

CALLS_URL = "https://api.ultravox.test/api/calls"
OK_RESPONSE = {"callId": "call-123", "joinUrl": "wss://uv.test/join/abc"}


def make_client(**cfg_overrides) -> UltravoxCallClient:
    return UltravoxCallClient(make_config(**cfg_overrides), log)


@pytest.fixture
def calls_api():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(CALLS_URL).mock(return_value=httpx.Response(201, json=OK_RESPONSE))
        yield route


def sent_body(route) -> dict:
    return json.loads(route.calls.last.request.content)


class TestRequestContract:
    async def test_default_payload_shape(self, calls_api):
        join_url = await make_client().create_ws_call_join_url()

        assert join_url == "wss://uv.test/join/abc"
        body = sent_body(calls_api)
        assert body == {
            # cfg default prompt + the voicemail guard (enabled by default)
            "systemPrompt": "You are a test assistant." + _VOICEMAIL_GUARD_PROMPT,
            "voice": "voice-global-test",                   # cfg fallback
            "temperature": 0.3,
            "firstSpeakerSettings": {
                # Outbound: callee speaks first; agent greets after the
                # fallback delay if the callee stays silent.
                "user": {
                    "fallback": {
                        "delay": "4s",
                        "prompt": "Greet the user and introduce yourself.",
                    }
                }
            },
            "joinTimeout": "60s",
            "recordingEnabled": True,
            # hangUp is what the voicemail guard instructs the agent to call
            "selectedTools": [{"toolName": "hangUp"}],
            "medium": {
                "serverWebSocket": {
                    "inputSampleRate": 16000,
                    "outputSampleRate": 16000,
                    "clientBufferSizeMs": 60,
                }
            },
        }
        assert "metadata" not in body
        assert "model" not in body  # empty cfg -> let the API pick its default
        assert "firstSpeaker" not in body  # deprecated in favor of firstSpeakerSettings

    async def test_voicemail_guard_follows_the_call_language(self, calls_api):
        # A long English block at the end of a pt-BR prompt pulls the model's
        # generation style (perceived accent) toward English — the guard must
        # be written in the call's language, selected by country.
        await make_client().create_ws_call_join_url(system_prompt="Você é a Ana.", country_code="BR")
        assert sent_body(calls_api)["systemPrompt"] == "Você é a Ana." + _VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY["BR"]
        assert "IMPORTANTE: Se a ligação" in _VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY["BR"]

        await make_client().create_ws_call_join_url(system_prompt="Eres Ana.", country_code="CL")
        assert sent_body(calls_api)["systemPrompt"] == "Eres Ana." + _VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY["CL"]
        assert "IMPORTANTE: Si la llamada" in _VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY["CL"]

        # Unknown/absent country falls back to the English guard.
        await make_client().create_ws_call_join_url(system_prompt="You are Ana.", country_code="XX")
        assert sent_body(calls_api)["systemPrompt"] == "You are Ana." + _VOICEMAIL_GUARD_PROMPT

    async def test_language_hint_sent_when_provided_and_omitted_otherwise(self, calls_api):
        await make_client().create_ws_call_join_url(language_hint="pt-BR")
        assert sent_body(calls_api)["languageHint"] == "pt-BR"

        # Empty/absent hint -> field omitted, Ultravox keeps auto-detecting
        # (this is the LANGUAGE_HINT_XX="" rollback path).
        await make_client().create_ws_call_join_url(language_hint="")
        assert "languageHint" not in sent_body(calls_api)

    async def test_explicit_prompt_and_voice_override_config(self, calls_api):
        await make_client().create_ws_call_join_url(
            system_prompt="Custom prompt", voice="voice-custom"
        )
        body = sent_body(calls_api)
        assert body["systemPrompt"] == "Custom prompt" + _VOICEMAIL_GUARD_PROMPT
        assert body["voice"] == "voice-custom"

    async def test_metadata_is_forwarded_when_provided(self, calls_api):
        metadata = {"tenantId": "t-1", "transport": "ULTRAVOX_SIP"}
        await make_client().create_ws_call_join_url(metadata=metadata)
        assert sent_body(calls_api)["metadata"] == metadata

    async def test_api_key_header_is_sent(self, calls_api):
        await make_client(ultravox_api_key="uvk_abc").create_ws_call_join_url()
        request = calls_api.calls.last.request
        assert request.headers["X-API-Key"] == "uvk_abc"

    async def test_greeting_message_becomes_fallback_text(self, calls_api):
        await make_client().create_ws_call_join_url(greeting_message="Olá, aqui é da Acme!")
        fallback = sent_body(calls_api)["firstSpeakerSettings"]["user"]["fallback"]
        assert fallback == {"delay": "4s", "text": "Olá, aqui é da Acme!"}
        assert "prompt" not in fallback

    async def test_temperature_and_join_timeout_follow_config(self, calls_api):
        await make_client(ultravox_temperature=0.7, ultravox_join_timeout="90s").create_ws_call_join_url()
        body = sent_body(calls_api)
        assert body["temperature"] == 0.7
        assert body["joinTimeout"] == "90s"

    async def test_per_call_temperature_overrides_config(self, calls_api):
        await make_client(ultravox_temperature=0.7).create_ws_call_join_url(temperature=0.1)
        assert sent_body(calls_api)["temperature"] == 0.1

    async def test_model_is_sent_only_when_configured(self, calls_api):
        await make_client(ultravox_model="ultravox-v0.7").create_ws_call_join_url()
        assert sent_body(calls_api)["model"] == "ultravox-v0.7"

    async def test_voicemail_hangup_disabled_leaves_prompt_and_tools_untouched(self, calls_api):
        await make_client(ultravox_voicemail_hangup=False).create_ws_call_join_url(
            system_prompt="Custom prompt"
        )
        body = sent_body(calls_api)
        assert body["systemPrompt"] == "Custom prompt"
        assert "selectedTools" not in body

    async def test_sample_rates_follow_config(self, calls_api):
        await make_client(sample_rate=48000).create_ws_call_join_url()
        ws = sent_body(calls_api)["medium"]["serverWebSocket"]
        assert ws["inputSampleRate"] == 48000
        assert ws["outputSampleRate"] == 48000


class TestValidation:
    async def test_missing_api_key_fails_before_any_request(self, calls_api):
        with pytest.raises(SystemExit, match="ULTRAVOX_API_KEY"):
            await make_client(ultravox_api_key="").create_ws_call_join_url()
        assert not calls_api.called

    async def test_no_voice_anywhere_fails_before_any_request(self, calls_api):
        with pytest.raises(SystemExit, match="voice"):
            await make_client(ultravox_voice="").create_ws_call_join_url()
        assert not calls_api.called


class TestResponseHandling:
    async def test_http_error_raises(self):
        with respx.mock:
            respx.post(CALLS_URL).mock(
                return_value=httpx.Response(402, json={"error": "quota exceeded"})
            )
            with pytest.raises(httpx.HTTPStatusError):
                await make_client().create_ws_call_join_url()

    async def test_missing_join_url_raises_runtime_error(self):
        with respx.mock:
            respx.post(CALLS_URL).mock(
                return_value=httpx.Response(201, json={"callId": "call-123"})
            )
            with pytest.raises(RuntimeError, match="joinUrl"):
                await make_client().create_ws_call_join_url()

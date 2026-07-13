"""Shared test fixtures and factories.

Environment-isolation rule for this suite
-----------------------------------------
`lk_ultravox_bridge.config` calls `load_dotenv(override=True)` at import time
and freezes both `_PROFILE_MAP` and the `BridgeConfig` dataclass defaults from
whatever environment (including a developer's local `.env`) is present at that
moment.  Tests must therefore NEVER assert on import-time defaults: always
construct `BridgeConfig(...)` / `CountryProfile(...)` with explicit kwargs, and
patch `config._PROFILE_MAP` when exercising `resolve_profile`.  The sentinel
tests in `tests/unit/test_env_isolation.py` guard this rule.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lk_ultravox_bridge.config import BridgeConfig, CountryProfile


def make_profile(**overrides) -> CountryProfile:
    """A fully-populated, valid CountryProfile for tests."""
    defaults = dict(
        country_code="BR",
        prefix="+55",
        provider="twilio",
        livekit_url="https://test-br.livekit.cloud",
        livekit_wss_url="wss://test-br.livekit.cloud",
        livekit_api_key="APItestkey",
        livekit_api_secret="testsecret-not-real-0123456789abcdef",
        sip_trunk_id="ST_test",
        sip_from_number="+5511999990000",
        ultravox_voice="voice-br-test",
        language_hint="pt-BR",
    )
    defaults.update(overrides)
    return CountryProfile(**defaults)


def make_config(**overrides) -> BridgeConfig:
    """A BridgeConfig with explicit, machine-independent values."""
    defaults = dict(
        ultravox_api_key="uvk_test_key",
        ultravox_calls_url="https://api.ultravox.test/api/calls",
        ultravox_voice="voice-global-test",
        ultravox_system_prompt="You are a test assistant.",
        ultravox_temperature=0.3,
        ultravox_model="",
        ultravox_join_timeout="60s",
        ultravox_greeting_delay="4s",
        ultravox_voicemail_hangup=True,
        sample_rate=16000,
        channels=1,
        frame_ms=20,
        max_buffer_frames=5,
        keep_buffer_frames=2,
        max_concurrent_calls=1,
        aws_region="us-east-1",
        aws_profile="test-profile",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_account_id="123456789012",
        sqs_queue_name="TestQueue",
    )
    defaults.update(overrides)
    return BridgeConfig(**defaults)


@pytest.fixture
def profile() -> CountryProfile:
    return make_profile()


@pytest.fixture
def cfg() -> BridgeConfig:
    return make_config()


class FakeWS:
    """Stands in for the Ultravox websocket.

    - Iterating yields `incoming` messages (bytes = audio, str = control JSON).
    - `send()` records outgoing payloads.
    - `iter_error` is raised after all messages are consumed.
    - `hang=True` blocks (cancellably) after the messages, simulating a live
      but quiet connection.
    """

    def __init__(self, incoming=None, *, send_error=None, iter_error=None, hang=False):
        self._incoming = list(incoming or [])
        self._send_error = send_error
        self._iter_error = iter_error
        self._hang = hang
        self.sent: list[bytes] = []

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for msg in self._incoming:
            yield msg
        if self._iter_error is not None:
            raise self._iter_error
        if self._hang:
            await asyncio.Event().wait()

    async def send(self, payload):
        if self._send_error is not None:
            raise self._send_error
        self.sent.append(payload)


class FakeAudioSource:
    """Records frames captured into LiveKit and clear_queue calls."""

    def __init__(self):
        self.captured: list[bytes] = []
        self.clear_queue_calls = 0

    async def capture_frame(self, frame):
        self.captured.append(bytes(frame.data.cast("B")))

    def clear_queue(self):
        self.clear_queue_calls += 1


class FakeAudioStream:
    """Stands in for rtc.AudioStream.from_track on the SIP->Ultravox leg."""

    def __init__(self, frames=None, *, hang=False):
        self._frames = list(frames or [])
        self._hang = hang
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for data in self._frames:
            yield SimpleNamespace(frame=SimpleNamespace(data=data))
        if self._hang:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True

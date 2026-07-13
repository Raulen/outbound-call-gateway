"""TriggerCallProcessor orchestration: what gets dialed, with which voice,
and the error semantics the SQS delete/retry loop depends on."""
from __future__ import annotations

import json
import logging

import pytest

import lk_ultravox_bridge.config as config_module
import lk_ultravox_bridge.sqs_worker as worker_module
from lk_ultravox_bridge.sqs_worker import TriggerCallProcessor

from tests.conftest import make_config, make_profile
from tests.unit.test_message_models import valid_payload

log = logging.getLogger("test")


class FakeAgent:
    instances: list = []

    def __init__(self, cfg, log, room_name, profile):
        self.room_name = room_name
        self.profile = profile
        self.connected = False
        self.bridged_join_url = None
        self.bridge_error = None
        FakeAgent.instances.append(self)

    async def connect_livekit(self):
        self.connected = True

    async def run_bridge(self, join_url):
        self.bridged_join_url = join_url
        if self.bridge_error:
            raise self.bridge_error


class FakeUltravox:
    def __init__(self):
        self.calls = []

    async def create_ws_call_join_url(self, *, system_prompt=None, voice=None, metadata=None,
                                      greeting_message=None):
        self.calls.append({"system_prompt": system_prompt, "voice": voice, "metadata": metadata,
                           "greeting_message": greeting_message})
        return "wss://uv.test/join/xyz"


class FakeDialer:
    def __init__(self, error=None):
        self.error = error
        self.dials = []
        self.completed = False

    async def dial_out(self, room_name, to_number, profile):
        self.dials.append((room_name, to_number, profile))
        self.completed = True
        if self.error:
            raise self.error


@pytest.fixture
def processor(monkeypatch):
    br = make_profile()
    cl = make_profile(country_code="CL", prefix="+56", provider="switch",
                      ultravox_voice="voice-cl-test")
    monkeypatch.setattr(config_module, "_PROFILE_MAP", {"+55": br, "+56": cl})
    monkeypatch.setattr(worker_module, "BridgeAgent", FakeAgent)
    FakeAgent.instances = []

    proc = TriggerCallProcessor(make_config(), log)
    proc._uv = FakeUltravox()
    proc._dialer = FakeDialer()
    proc.profiles = {"BR": br, "CL": cl}
    return proc


class TestBuildUltravoxMetadata:
    def test_maps_tracking_fields_and_transport(self, processor):
        metadata = processor.build_ultravox_metadata(valid_payload())
        assert metadata == {
            "organizationId": "org-1",
            "tenantId": "tenant-1",
            "workflowId": "wf-1",
            "campaignId": "cmp-1",
            "customerId": "cust-1",
            "callId": "msg-001",  # falls back to payload id
            "userId": "user-1",
            "transport": "ULTRAVOX_SIP",
        }

    def test_call_id_precedence_metadata_over_payload_over_id(self, processor):
        payload = valid_payload(callId="payload-call")
        payload["metadata"]["callId"] = "metadata-call"
        assert processor.build_ultravox_metadata(payload)["callId"] == "metadata-call"

        payload = valid_payload(callId="payload-call")
        assert processor.build_ultravox_metadata(payload)["callId"] == "payload-call"

        assert processor.build_ultravox_metadata(valid_payload())["callId"] == "msg-001"


class TestProcessBodyHappyPath:
    async def test_full_flow_wires_prompt_voice_metadata_and_dial(self, processor):
        await processor.process_body(json.dumps(valid_payload()))

        # Ultravox call: prompt from the message, voice from the BR profile,
        # tracking metadata forwarded.
        uv_call = processor._uv.calls[0]
        assert uv_call["system_prompt"] == "You are a collections agent."
        assert uv_call["voice"] == "voice-br-test"
        assert uv_call["metadata"]["transport"] == "ULTRAVOX_SIP"
        assert uv_call["greeting_message"] == "Olá!"  # from prompt.greetingMessage

        # Agent: connected and bridged with the join URL.
        agent = FakeAgent.instances[0]
        assert agent.connected
        assert agent.bridged_join_url == "wss://uv.test/join/xyz"
        assert agent.room_name.startswith("call-")
        assert agent.profile is processor.profiles["BR"]

        # Dial: same room, primary number, same profile.
        assert processor._dialer.dials == [
            (agent.room_name, "+5511999998888", processor.profiles["BR"])
        ]

    async def test_message_voice_id_overrides_profile_voice(self, processor):
        payload = valid_payload()
        payload["metadata"]["voiceId"] = "voice-from-message"
        await processor.process_body(json.dumps(payload))
        assert processor._uv.calls[0]["voice"] == "voice-from-message"

    async def test_chile_number_routes_to_cl_profile(self, processor):
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = [{"number": "56912345678", "order": 1}]
        await processor.process_body(json.dumps(payload))
        assert processor._uv.calls[0]["voice"] == "voice-cl-test"
        assert processor._dialer.dials[0][2] is processor.profiles["CL"]


class TestProcessBodyErrorSemantics:
    """process_body raising == the worker will NOT delete the SQS message."""

    async def test_invalid_json_raises(self, processor):
        with pytest.raises(json.JSONDecodeError):
            await processor.process_body("{not json")

    async def test_wrong_message_type_raises(self, processor):
        with pytest.raises(ValueError, match="messageType"):
            await processor.process_body(json.dumps(valid_payload(messageType="OTHER")))
        assert processor._uv.calls == []  # rejected before any side effect

    async def test_bridge_failure_propagates_but_dial_task_is_awaited(self, processor, monkeypatch):
        async def failing_run_bridge(self, join_url):
            raise ConnectionError("bridge died")

        monkeypatch.setattr(FakeAgent, "run_bridge", failing_run_bridge)

        with pytest.raises(ConnectionError, match="bridge died"):
            await processor.process_body(json.dumps(valid_payload()))

        # The background dial task must have been awaited (finally block),
        # not abandoned with a pending-task warning.
        assert processor._dialer.completed

    async def test_dial_failure_is_swallowed_and_logged(self, processor, caplog):
        processor._dialer = FakeDialer(error=ConnectionError("trunk 403"))
        with caplog.at_level(logging.ERROR):
            await processor.process_body(json.dumps(valid_payload()))  # must not raise
        assert "dial task failed" in caplog.text

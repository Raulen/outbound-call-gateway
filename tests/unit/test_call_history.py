"""CALL_HISTORY contract: message shape, per-scenario emission sequences,
best-effort delivery, and the event_samples/ drift guard."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path

import pytest

import lk_ultravox_bridge.config as config_module
import lk_ultravox_bridge.sqs_worker as worker_module
from lk_ultravox_bridge.call_history import (
    EMITTED_STATUSES,
    CallHistoryEmitter,
    NullCallHistoryPublisher,
    SqsCallHistoryPublisher,
    build_call_history_publisher,
    uuid7,
)
from lk_ultravox_bridge.livekit_client import CallNotAnsweredError
from lk_ultravox_bridge.sqs_worker import TriggerCallProcessor
from lk_ultravox_bridge.ultravox_client import UltravoxCall

from tests.conftest import make_config, make_profile
from tests.unit.test_message_models import valid_payload

log = logging.getLogger("test")

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "event_samples"

ENVELOPE_KEYS = {"id", "messageType", "source", "organizationId", "tenantId", "createdAt", "metadata"}
METADATA_KEYS = {"workflowId", "campaignId", "customerId", "userId", "callId",
                 "status", "statusDescription", "metadataJson"}


class RecordingPublisher:
    def __init__(self):
        self.published: list[dict] = []

    async def publish(self, body: dict) -> None:
        self.published.append(body)


def make_emitter(publisher=None, **tracking_overrides) -> CallHistoryEmitter:
    tracking = {
        "organizationId": "org-1", "tenantId": "tenant-1", "workflowId": "wf-1",
        "campaignId": "cmp-1", "customerId": "cust-1", "callId": "call-1",
        "userId": "user-1", "transport": "ULTRAVOX_SIP",
    }
    tracking.update(tracking_overrides)
    return CallHistoryEmitter(publisher if publisher is not None else RecordingPublisher(), log, tracking)


class TestUuid7:
    def test_is_a_valid_version_7_uuid(self):
        val = uuid.UUID(uuid7())
        assert val.version == 7
        assert val.variant == uuid.RFC_4122

    def test_is_time_ordered_across_milliseconds(self):
        # v7 embeds a unix-ms timestamp in the top 48 bits, so ids from
        # different instants sort chronologically (ties within the same ms
        # are unordered, same as createdAt — fine for the consumer).
        import time
        first = uuid7()
        time.sleep(0.002)
        second = uuid7()
        assert uuid.UUID(first).int >> 80 < uuid.UUID(second).int >> 80
        assert first < second


class TestMessageContract:
    async def test_envelope_and_metadata_shape(self):
        pub = RecordingPublisher()
        await make_emitter(pub).emit("SIP_DIAL_ANSWERED", "SIP dial answered")

        (body,) = pub.published
        assert set(body) == ENVELOPE_KEYS
        assert set(body["metadata"]) == METADATA_KEYS
        assert body["messageType"] == "CALL_HISTORY"
        assert body["source"] == "outbound-call-gateway"
        assert body["organizationId"] == "org-1"
        assert body["tenantId"] == "tenant-1"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", body["createdAt"])
        md = body["metadata"]
        assert md["status"] == "SIP_DIAL_ANSWERED"
        assert md["statusDescription"] == "SIP dial answered"
        assert md["callId"] == "call-1"
        assert md["userId"] == "user-1"  # echoed from TRIGGER_CALL as-is

    async def test_base_metadata_is_merged_into_every_event(self):
        pub = RecordingPublisher()
        emitter = CallHistoryEmitter(
            pub, log,
            {"organizationId": "org-1", "tenantId": "t-1", "customerId": "c-1", "callId": "call-1"},
            base_metadata={"room": "call-abc", "toNumber": "+551199", "country": "BR", "provider": "twilio"},
        )
        await emitter.emit("SIP_CALL_ENDED", "Call ended", {"durationSeconds": 10, "endReason": "callee-hangup"})
        md = json.loads(pub.published[0]["metadata"]["metadataJson"])
        assert md == {
            "room": "call-abc", "toNumber": "+551199", "country": "BR", "provider": "twilio",
            "durationSeconds": 10, "endReason": "callee-hangup",
        }

    async def test_metadata_json_is_a_double_encoded_string(self):
        # Contract: metadataJson is a JSON *string*, not a nested object.
        pub = RecordingPublisher()
        await make_emitter(pub).emit(
            "SIP_CALL_ENDED", "Call ended", {"durationSeconds": 95, "endReason": "callee-hangup"}
        )
        raw = pub.published[0]["metadata"]["metadataJson"]
        assert isinstance(raw, str)
        assert json.loads(raw) == {"durationSeconds": 95, "endReason": "callee-hangup"}

    async def test_empty_metadata_defaults_to_empty_json_object(self):
        pub = RecordingPublisher()
        await make_emitter(pub).emit("CALL_ATTEMPT_STARTED", "Dial attempt started")
        assert pub.published[0]["metadata"]["metadataJson"] == "{}"

    async def test_unknown_status_is_rejected(self):
        with pytest.raises(AssertionError, match="unknown CALL_HISTORY status"):
            await make_emitter().emit("CALL_BILLED", "not ours to emit")

    async def test_blank_call_id_warns_but_still_emits(self, caplog):
        # The consumer discards blank callId; the warn makes that visible here.
        pub = RecordingPublisher()
        with caplog.at_level(logging.WARNING):
            emitter = make_emitter(pub, callId=None)
        assert "blank callId" in caplog.text
        await emitter.emit("SIP_DIAL_ANSWERED", "SIP dial answered")
        assert pub.published  # still emitted; discarding is the consumer's call


class TestBestEffortDelivery:
    async def test_sqs_publish_failure_never_raises(self, caplog):
        class ExplodingSqs:
            def send_message(self, **kwargs):
                raise ConnectionError("sqs down")

        pub = SqsCallHistoryPublisher(ExplodingSqs(), "https://sqs.test/q", log)
        with caplog.at_level(logging.WARNING):
            await pub.publish({"metadata": {"status": "SIP_DIAL_ANSWERED", "callId": "c1"}})
        assert "publish failed" in caplog.text  # logged, swallowed

    async def test_sqs_publisher_sends_raw_json_body(self):
        sent = {}

        class CapturingSqs:
            def send_message(self, *, QueueUrl, MessageBody):
                sent.update(QueueUrl=QueueUrl, MessageBody=MessageBody)

        await SqsCallHistoryPublisher(CapturingSqs(), "https://sqs.test/q", log).publish({"a": 1})
        assert sent["QueueUrl"] == "https://sqs.test/q"
        assert json.loads(sent["MessageBody"]) == {"a": 1}  # raw body, no envelope

    def test_factory_disabled_without_queue_name(self):
        cfg = make_config(call_history_queue_name="")
        assert isinstance(build_call_history_publisher(cfg, object(), log), NullCallHistoryPublisher)

    def test_factory_builds_queue_url_from_account_and_region(self):
        cfg = make_config(call_history_queue_name="CallHistoryQueue")
        pub = build_call_history_publisher(cfg, object(), log)
        assert isinstance(pub, SqsCallHistoryPublisher)
        assert pub._queue_url == "https://sqs.us-east-1.amazonaws.com/123456789012/CallHistoryQueue"


# ---------------------------------------------------------------------------
# Emission sequences: one call = one specific ordered status story.
# ---------------------------------------------------------------------------

class SequenceFakeAgent:
    """Like test_sqs_worker.FakeAgent, but honors on_bridge_active and
    exposes an end_reason like the real BridgeAgent."""

    instances: list = []
    default_bridge_error = None  # set by tests to make run_bridge raise

    def __init__(self, cfg, log, room_name, profile):
        self.room_name = room_name
        self.on_bridge_active = None
        self.end_reason = None
        self.bridge_error = SequenceFakeAgent.default_bridge_error
        self.bridge_end_reason = "callee-hangup"
        SequenceFakeAgent.instances.append(self)

    async def connect_livekit(self):
        pass

    async def run_bridge(self, join_url, *, remote_track_timeout=None):
        if self.on_bridge_active is not None:
            await self.on_bridge_active()
        if self.bridge_error:
            raise self.bridge_error
        self.end_reason = self.bridge_end_reason

    async def teardown(self):
        pass


class FakeUltravox:
    async def create_ws_call_join_url(self, **kwargs):
        return UltravoxCall(join_url="wss://uv.test/join/xyz", call_id="uv-call-1")


class FakeDialer:
    def __init__(self, error=None):
        self.error = error

    async def dial_out(self, room_name, to_number, profile):
        if self.error:
            raise self.error


@pytest.fixture
def sequenced(monkeypatch):
    br = make_profile()
    monkeypatch.setattr(config_module, "_PROFILE_MAP", {"+55": br, "+56": br})
    monkeypatch.setattr(worker_module, "BridgeAgent", SequenceFakeAgent)
    SequenceFakeAgent.instances = []
    SequenceFakeAgent.default_bridge_error = None

    pub = RecordingPublisher()
    proc = TriggerCallProcessor(make_config(), log, pub)
    proc._uv = FakeUltravox()
    proc._dialer = FakeDialer()
    return proc, pub


def statuses(pub: RecordingPublisher) -> list[str]:
    return [b["metadata"]["status"] for b in pub.published]


def ended_metadata(pub: RecordingPublisher) -> dict:
    assert pub.published[-1]["metadata"]["status"] == "SIP_CALL_ENDED"
    return json.loads(pub.published[-1]["metadata"]["metadataJson"])


class TestEmissionSequences:
    async def test_answered_call_emits_the_full_happy_sequence(self, sequenced):
        proc, pub = sequenced
        await proc.process_body(json.dumps(valid_payload()))
        assert statuses(pub) == [
            "CALL_ATTEMPT_STARTED", "SIP_DIAL_ANSWERED", "SIP_BRIDGE_ACTIVE", "SIP_CALL_ENDED",
        ]
        md = ended_metadata(pub)
        assert md["endReason"] == "callee-hangup"
        assert isinstance(md["durationSeconds"], int)
        assert md["ultravoxCallId"] == "uv-call-1"

        # Every event carries the gateway-only call context.
        for body in pub.published:
            base = json.loads(body["metadata"]["metadataJson"])
            assert base["toNumber"] == "+5511999998888"
            assert base["country"] == "BR"
            assert base["provider"] == "twilio"
            assert base["room"].startswith("call-")

        started = json.loads(pub.published[1]["metadata"]["metadataJson"])
        assert isinstance(started["answerDelaySeconds"], int)

    async def test_events_echo_trigger_call_ids(self, sequenced):
        proc, pub = sequenced
        await proc.process_body(json.dumps(valid_payload()))
        md = pub.published[0]["metadata"]
        assert md["callId"] == "msg-001"       # same resolution as ultravox metadata
        assert md["customerId"] == "cust-1"
        assert md["userId"] == "user-1"        # echoed as-is (backend's source of truth)

    async def test_unanswered_call_emits_not_answered_with_reason(self, sequenced):
        proc, pub = sequenced
        proc._dialer = FakeDialer(error=CallNotAnsweredError("busy", 486))
        await proc.process_body(json.dumps(valid_payload()))
        assert statuses(pub) == ["CALL_ATTEMPT_STARTED", "CALL_NOT_ANSWERED"]
        md = json.loads(pub.published[-1]["metadata"]["metadataJson"])
        assert md["reason"] == "busy"
        assert md["sipStatus"] == 486
        assert md["toNumber"] == "+5511999998888"  # base context rides along

    async def test_system_error_before_dial_emits_only_call_failed(self, sequenced):
        proc, pub = sequenced

        class ExplodingUltravox:
            async def create_ws_call_join_url(self, **kwargs):
                raise ConnectionError("uv 500")

        proc._uv = ExplodingUltravox()
        with pytest.raises(ConnectionError):
            await proc.process_body(json.dumps(valid_payload()))
        assert statuses(pub) == ["SIP_CALL_FAILED"]  # no attempt started: nothing dialed

    async def test_system_error_on_dial_emits_attempt_then_failed(self, sequenced):
        proc, pub = sequenced
        proc._dialer = FakeDialer(error=ConnectionError("trunk 403"))
        with pytest.raises(ConnectionError):
            await proc.process_body(json.dumps(valid_payload()), receive_count=3)
        assert statuses(pub) == ["CALL_ATTEMPT_STARTED", "SIP_CALL_FAILED"]
        md = json.loads(pub.published[-1]["metadata"]["metadataJson"])
        assert md["reason"] == "system-error"
        assert md["errorType"] == "ConnectionError"  # triage without opening logs
        assert md["attempt"] == 3                    # ApproximateReceiveCount (DLQ at 5)

    async def test_call_failed_omits_attempt_when_receive_count_unknown(self, sequenced):
        proc, pub = sequenced
        proc._dialer = FakeDialer(error=ConnectionError("trunk 403"))
        with pytest.raises(ConnectionError):
            await proc.process_body(json.dumps(valid_payload()))
        assert "attempt" not in json.loads(pub.published[-1]["metadata"]["metadataJson"])

    async def test_not_answered_without_sip_code_omits_the_key(self, sequenced):
        # dial-timeout has no SIP code; the Digicob compiler must see the key
        # absent, never null/invented.
        proc, pub = sequenced
        proc._dialer = FakeDialer(error=CallNotAnsweredError("dial-timeout", None))
        await proc.process_body(json.dumps(valid_payload()))
        md = json.loads(pub.published[-1]["metadata"]["metadataJson"])
        assert md["reason"] == "dial-timeout"
        assert "sipStatus" not in md

    async def test_call_failed_carries_sip_status_when_extractable(self, sequenced):
        # An unmapped SIP failure (e.g. 503 from the trunk) is a system error,
        # but its code still matters for the Digicob return file.
        proc, pub = sequenced

        class TwirpLikeError(Exception):
            metadata = {"sip_status_code": "503"}
            message = "twirp error unavailable: INVITE failed: sip status: 503"

        proc._dialer = FakeDialer(error=TwirpLikeError())
        with pytest.raises(TwirpLikeError):
            await proc.process_body(json.dumps(valid_payload()))
        md = json.loads(pub.published[-1]["metadata"]["metadataJson"])
        assert pub.published[-1]["metadata"]["status"] == "SIP_CALL_FAILED"
        assert md["sipStatus"] == 503

    async def test_call_failed_omits_sip_status_when_absent(self, sequenced):
        proc, pub = sequenced
        proc._dialer = FakeDialer(error=ConnectionError("network down"))
        with pytest.raises(ConnectionError):
            await proc.process_body(json.dumps(valid_payload()))
        assert "sipStatus" not in json.loads(pub.published[-1]["metadata"]["metadataJson"])

    async def test_bridge_death_after_answer_is_call_ended_not_failed(self, sequenced):
        # The callee answered and talked: talk time is real and the message is
        # already acked (no retry), so this must never move the customer to
        # "failed" — it is a SIP_CALL_ENDED with an abnormal endReason.
        proc, pub = sequenced
        SequenceFakeAgent.default_bridge_error = ConnectionError("ws died")

        with pytest.raises(ConnectionError):
            await proc.process_body(json.dumps(valid_payload()))

        assert statuses(pub) == [
            "CALL_ATTEMPT_STARTED", "SIP_DIAL_ANSWERED", "SIP_BRIDGE_ACTIVE", "SIP_CALL_ENDED",
        ]
        assert ended_metadata(pub)["endReason"] == "bridge-error"


# ---------------------------------------------------------------------------
# event_samples/ drift guard: the folder is the tracked model of what this
# gateway emits; it must always match the code.
# ---------------------------------------------------------------------------

class TestEventSamplesTracking:
    def sample_files(self) -> dict[str, dict]:
        return {p.stem: json.loads(p.read_text(encoding="utf-8"))
                for p in SAMPLES_DIR.glob("*.json")}

    def test_one_sample_per_emitted_status_no_more_no_less(self):
        assert set(self.sample_files()) == set(EMITTED_STATUSES)

    def test_samples_match_the_wire_shape(self):
        for name, body in self.sample_files().items():
            assert set(body) == ENVELOPE_KEYS, name
            assert set(body["metadata"]) == METADATA_KEYS, name
            assert body["messageType"] == "CALL_HISTORY", name
            assert body["source"] == "outbound-call-gateway", name
            assert body["metadata"]["status"] == name
            assert uuid.UUID(body["id"]).version == 7, name
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", body["createdAt"]), name
            parsed = json.loads(body["metadata"]["metadataJson"])
            assert isinstance(parsed, dict), name
            # Gateway-only call context, present in every event.
            for key in ("room", "toNumber", "country", "provider"):
                assert key in parsed, f"{name} sample missing base field {key!r}"

    def test_call_ended_sample_documents_duration_and_end_reason(self):
        md = json.loads(self.sample_files()["SIP_CALL_ENDED"]["metadata"]["metadataJson"])
        assert isinstance(md["durationSeconds"], int)
        assert re.fullmatch(r"[a-z-]+", md["endReason"])  # Grafana endReason regexp

    def test_not_answered_sample_documents_reason_and_sip_status(self):
        md = json.loads(self.sample_files()["CALL_NOT_ANSWERED"]["metadata"]["metadataJson"])
        assert re.fullmatch(r"[a-z-]+", md["reason"])
        assert isinstance(md["sipStatus"], int)

    def test_call_started_sample_documents_answer_delay(self):
        md = json.loads(self.sample_files()["SIP_DIAL_ANSWERED"]["metadata"]["metadataJson"])
        assert isinstance(md["answerDelaySeconds"], int)

    def test_call_failed_sample_documents_error_type_and_attempt(self):
        md = json.loads(self.sample_files()["SIP_CALL_FAILED"]["metadata"]["metadataJson"])
        assert md["errorType"]
        assert isinstance(md["attempt"], int)

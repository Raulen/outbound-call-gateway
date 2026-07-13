"""TriggerCallProcessor orchestration: what gets dialed, with which voice,
and the error semantics the SQS delete/retry loop depends on."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest

import lk_ultravox_bridge.config as config_module
import lk_ultravox_bridge.sqs_worker as worker_module
from lk_ultravox_bridge.sqs_consumer import SqsMessage
from lk_ultravox_bridge.sqs_worker import TriggerCallProcessor, run_worker_loop

from tests.conftest import make_config, make_profile
from tests.unit.test_message_models import valid_payload

log = logging.getLogger("test")


# Ordered record of side effects across the fakes, so tests can assert the
# ack happens after the dial is answered and before the bridge runs.
EVENTS: list = []


class FakeAgent:
    instances: list = []

    def __init__(self, cfg, log, room_name, profile):
        self.room_name = room_name
        self.profile = profile
        self.log = log
        self.connected = False
        self.bridged_join_url = None
        self.remote_track_timeout = None
        self.bridge_error = None
        self.torn_down = False
        FakeAgent.instances.append(self)

    async def connect_livekit(self):
        self.connected = True

    async def run_bridge(self, join_url, *, remote_track_timeout=None):
        EVENTS.append("bridge")
        self.bridged_join_url = join_url
        self.remote_track_timeout = remote_track_timeout
        if self.bridge_error:
            raise self.bridge_error

    async def teardown(self):
        self.torn_down = True


class FakeUltravox:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def create_ws_call_join_url(self, *, system_prompt=None, voice=None, metadata=None,
                                      greeting_message=None, country_code=None, language_hint=None):
        self.calls.append({"system_prompt": system_prompt, "voice": voice, "metadata": metadata,
                           "greeting_message": greeting_message, "country_code": country_code,
                           "language_hint": language_hint})
        if self.error:
            raise self.error
        return "wss://uv.test/join/xyz"


class FakeDialer:
    def __init__(self, error=None, hang=False):
        self.error = error
        self.hang = hang  # simulate a phone that rings forever (never answered)
        self.dials = []

    async def dial_out(self, room_name, to_number, profile):
        self.dials.append((room_name, to_number, profile))
        if self.hang:
            await asyncio.sleep(3600)
        if self.error:
            raise self.error
        EVENTS.append("answered")


class AckRecorder:
    def __init__(self, error=None):
        self.error = error
        self.count = 0

    async def __call__(self):
        EVENTS.append("ack")
        self.count += 1
        if self.error:
            raise self.error


@pytest.fixture
def processor(monkeypatch):
    br = make_profile()
    cl = make_profile(country_code="CL", prefix="+56", provider="switch",
                      ultravox_voice="voice-cl-test", language_hint="es-CL")
    monkeypatch.setattr(config_module, "_PROFILE_MAP", {"+55": br, "+56": cl})
    monkeypatch.setattr(worker_module, "BridgeAgent", FakeAgent)
    FakeAgent.instances = []
    EVENTS.clear()

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
        assert uv_call["country_code"] == "BR"  # voicemail guard language follows the profile
        assert uv_call["language_hint"] == "pt-BR"  # ASR/TTS hint follows the profile

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

        # The worker bounds the wait for the SIP track (answered dial ⇒ the
        # track must show up fast; without this a stuck track hangs the worker).
        assert agent.remote_track_timeout == worker_module.REMOTE_TRACK_TIMEOUT_S

    async def test_ack_happens_after_answer_and_before_bridge(self, processor):
        ack = AckRecorder()
        await processor.process_body(json.dumps(valid_payload()), ack)
        assert EVENTS == ["answered", "ack", "bridge"]
        assert ack.count == 1

    async def test_agent_gets_a_call_scoped_logger(self, processor):
        # Concurrent calls interleave in one log; the agent (and everything it
        # builds — RTC, audio bridge, teardown) must log with call context.
        from lk_ultravox_bridge.logging_utils import CallLogAdapter

        await processor.process_body(json.dumps(valid_payload()))
        agent = FakeAgent.instances[0]
        assert isinstance(agent.log, CallLogAdapter)
        assert agent.log.extra["call_id"] == "msg-001"
        assert agent.log.extra["room"] == agent.room_name

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
        assert processor._uv.calls[0]["language_hint"] == "es-CL"
        assert processor._dialer.dials[0][2] is processor.profiles["CL"]


class TestProcessBodyErrorSemantics:
    """Before the dial is answered, raising == the message is retried (no ack).
    After answer, the message is acked first and failures only end this call."""

    async def test_invalid_json_raises(self, processor):
        with pytest.raises(json.JSONDecodeError):
            await processor.process_body("{not json")

    async def test_wrong_message_type_raises(self, processor):
        with pytest.raises(ValueError, match="messageType"):
            await processor.process_body(json.dumps(valid_payload(messageType="OTHER")))
        assert processor._uv.calls == []  # rejected before any side effect

    async def test_ultravox_failure_raises_without_ack_and_tears_down(self, processor):
        processor._uv = FakeUltravox(error=ConnectionError("uv 500"))
        ack = AckRecorder()

        with pytest.raises(ConnectionError, match="uv 500"):
            await processor.process_body(json.dumps(valid_payload()), ack)

        assert ack.count == 0  # message stays in the queue → retried
        assert FakeAgent.instances[0].torn_down  # room not leaked

    async def test_dial_failure_raises_without_ack_and_tears_down(self, processor):
        # e.g. trunk 403: retryable — nobody's phone rang to completion.
        processor._dialer = FakeDialer(error=ConnectionError("trunk 403"))
        ack = AckRecorder()

        with pytest.raises(ConnectionError, match="trunk 403"):
            await processor.process_body(json.dumps(valid_payload()), ack)

        assert ack.count == 0
        assert FakeAgent.instances[0].torn_down
        assert "bridge" not in EVENTS  # never bridged an unanswered call

    async def test_unanswered_dial_times_out_and_frees_the_worker(self, processor, monkeypatch):
        # Phone ringing forever must not hang the worker (pre-fix behavior).
        monkeypatch.setattr(worker_module, "DIAL_ANSWER_TIMEOUT_S", 0.01)
        processor._dialer = FakeDialer(hang=True)
        ack = AckRecorder()

        with pytest.raises(asyncio.TimeoutError):
            await processor.process_body(json.dumps(valid_payload()), ack)

        assert ack.count == 0
        assert FakeAgent.instances[0].torn_down

    async def test_bridge_failure_after_answer_still_acks(self, processor, monkeypatch):
        # The callee answered: a retry would double-call them, so the message
        # must already be acked even though the bridge died.
        ack = AckRecorder()

        async def failing_run_bridge(self, join_url, *, remote_track_timeout=None):
            EVENTS.append("bridge")
            raise ConnectionError("bridge died")

        monkeypatch.setattr(FakeAgent, "run_bridge", failing_run_bridge)

        with pytest.raises(ConnectionError, match="bridge died"):
            await processor.process_body(json.dumps(valid_payload()), ack)

        assert EVENTS == ["answered", "ack", "bridge"]
        assert ack.count == 1

    async def test_ack_failure_does_not_kill_the_live_call(self, processor, caplog):
        # SQS delete hiccup after answer: log it and keep the call going.
        ack = AckRecorder(error=ConnectionError("sqs down"))
        with caplog.at_level(logging.ERROR):
            await processor.process_body(json.dumps(valid_payload()), ack)  # must not raise
        assert "ack failed after answer" in caplog.text
        assert EVENTS == ["answered", "ack", "bridge"]  # bridge still ran

    async def test_process_body_without_ack_is_still_supported(self, processor):
        # CLI/tests may call without an ack callback.
        await processor.process_body(json.dumps(valid_payload()))
        assert EVENTS == ["answered", "bridge"]


class QueueOfBodies:
    """Sync fake of SqsLongPollConsumer fed by a list of message bodies."""

    def __init__(self, bodies):
        self._pending = list(bodies)
        self.deleted = []

    def receive(self, max_messages, wait_seconds, visibility_timeout):
        if self._pending:
            body = self._pending.pop(0)
            return [SqsMessage(receipt_handle=f"rh-{body}", body=body, attributes={})]
        import time
        time.sleep(0.005)  # idle long-poll: avoid a busy spin in the test loop
        return []

    def delete(self, receipt_handle):
        self.deleted.append(receipt_handle)


class BlockingProcessor:
    """Records started calls and holds each one until the test releases it."""

    def __init__(self):
        self.started: list = []
        self.release: dict = {}

    async def process_body(self, body, ack=None):
        evt = asyncio.Event()
        self.release[body] = evt
        self.started.append(body)
        await evt.wait()
        if ack is not None:
            await ack()


async def wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


class TestRunWorkerLoopConcurrency:
    """The Etapa 3 contract: calls run in parallel up to MAX_CONCURRENT_CALLS,
    and a message is only pulled from SQS when a slot is free."""

    async def test_calls_run_concurrently_up_to_the_cap(self):
        consumer = QueueOfBodies(["m1", "m2", "m3", "m4"])
        proc = BlockingProcessor()
        cfg = make_config(max_concurrent_calls=2)

        loop_task = asyncio.create_task(run_worker_loop(cfg, log, consumer, proc))
        try:
            # Two calls start without either finishing — true parallelism.
            await wait_until(lambda: len(proc.started) == 2)
            assert proc.started == ["m1", "m2"]

            # The 3rd message must NOT be pulled while both slots are busy.
            await asyncio.sleep(0.1)
            assert len(proc.started) == 2

            # Freeing one slot lets exactly one more message in.
            proc.release["m1"].set()
            await wait_until(lambda: len(proc.started) == 3)
            assert proc.started[2] == "m3"

            # Drain the rest.
            for body in ("m2", "m3"):
                proc.release[body].set()
            await wait_until(lambda: len(proc.started) == 4)
            proc.release["m4"].set()
            await wait_until(lambda: len(consumer.deleted) == 4)
        finally:
            loop_task.cancel()

        # Every finished call acked (deleted) its own message, no cross-wiring.
        assert sorted(consumer.deleted) == ["rh-m1", "rh-m2", "rh-m3", "rh-m4"]

    async def test_serial_mode_processes_one_at_a_time(self):
        # MAX_CONCURRENT_CALLS=1 is the rollback switch: strictly serial.
        consumer = QueueOfBodies(["m1", "m2"])
        proc = BlockingProcessor()
        cfg = make_config(max_concurrent_calls=1)

        loop_task = asyncio.create_task(run_worker_loop(cfg, log, consumer, proc))
        try:
            await wait_until(lambda: len(proc.started) == 1)
            await asyncio.sleep(0.1)
            assert proc.started == ["m1"]  # m2 waits for m1 to finish

            proc.release["m1"].set()
            await wait_until(lambda: len(proc.started) == 2)
            proc.release["m2"].set()
            await wait_until(lambda: len(consumer.deleted) == 2)
        finally:
            loop_task.cancel()

    async def test_receive_error_backs_off_and_keeps_polling(self, monkeypatch, caplog):
        # A network blip on the SQS poll must never kill the worker process —
        # this exact crash (EndpointConnectionError) took the worker down live.
        monkeypatch.setattr(worker_module, "POLL_ERROR_BACKOFF_S", 0.01)

        class FlakyConsumer(QueueOfBodies):
            def __init__(self, bodies):
                super().__init__(bodies)
                self.failures = 2

            def receive(self, max_messages, wait_seconds, visibility_timeout):
                if self.failures:
                    self.failures -= 1
                    raise ConnectionError("Could not connect to the endpoint URL")
                return super().receive(max_messages, wait_seconds, visibility_timeout)

        consumer = FlakyConsumer(["m1"])
        proc = BlockingProcessor()
        cfg = make_config(max_concurrent_calls=1)

        loop_task = asyncio.create_task(run_worker_loop(cfg, log, consumer, proc))
        try:
            with caplog.at_level(logging.WARNING):
                await wait_until(lambda: proc.started == ["m1"])
            assert "receive failed; retrying" in caplog.text

            proc.release["m1"].set()
            await wait_until(lambda: consumer.deleted == ["rh-m1"])
        finally:
            loop_task.cancel()

    async def test_processing_failure_does_not_stop_the_loop(self, caplog):
        class ExplodingThenBlockingProcessor(BlockingProcessor):
            async def process_body(self, body, ack=None):
                if body == "bad":
                    self.started.append(body)
                    raise ValueError("boom")
                await super().process_body(body, ack)

        consumer = QueueOfBodies(["bad", "good"])
        proc = ExplodingThenBlockingProcessor()
        cfg = make_config(max_concurrent_calls=1)

        loop_task = asyncio.create_task(run_worker_loop(cfg, log, consumer, proc))
        try:
            with caplog.at_level(logging.ERROR):
                await wait_until(lambda: "good" in proc.started)
            assert "message processing failed" in caplog.text
            assert "rh-bad" not in consumer.deleted  # failed → stays in queue

            proc.release["good"].set()
            await wait_until(lambda: consumer.deleted == ["rh-good"])
        finally:
            loop_task.cancel()

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Awaitable, Callable, Dict, Any, Optional

from dotenv import load_dotenv

# Load .env before importing BridgeConfig and other modules that depend on environment.
load_dotenv(override=True)

from .config import BridgeConfig
from .logging_utils import CallLogAdapter, ConfigDumper
from .sqs_consumer import SqsClientFactory, SqsQueueResolver, SqsLongPollConsumer
from .message_models import TriggerCallMessageParser
from .ultravox_client import UltravoxCallClient
from .livekit_client import CallNotAnsweredError, LiveKitSipDialer, extract_sip_status
from .agent import BridgeAgent
from .call_history import CallHistoryEmitter, NullCallHistoryPublisher, build_call_history_publisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sqs-worker")

# Upper bound for SIP dial-out + ringing.  dial_out uses wait_until_answered,
# so it returns only when the callee picks up; carriers typically give up at
# ~60s of ringing.  On timeout the message is retried (nobody was reached).
DIAL_ANSWER_TIMEOUT_S = 90.0
# Once the dial is answered the SIP audio track must surface within seconds;
# this bounds run_bridge's wait so a stuck track can never hang the worker.
REMOTE_TRACK_TIMEOUT_S = 30.0
# Wait before retrying after an SQS receive error (network blip, DNS, etc.).
# The poll must survive transient failures — in-flight calls depend on this
# process staying alive.
POLL_ERROR_BACKOFF_S = 5.0
# Liveness heartbeat cadence.  The "[HB] alive" line is what the Grafana
# "worker is down" alert watches; it also carries the in-flight gauge.
HEARTBEAT_INTERVAL_S = 60.0


class TriggerCallProcessor:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger, event_publisher=None):
        self._cfg = cfg
        self._log = log
        self._parser = TriggerCallMessageParser()
        self._uv = UltravoxCallClient(cfg, log)
        self._dialer = LiveKitSipDialer(log)
        self._events = event_publisher or NullCallHistoryPublisher()

    def build_ultravox_metadata(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        md = payload.get("metadata") or {}

        call_id = md.get("callId") or payload.get("callId") or payload.get("id")

        return {
            "organizationId": payload.get("organizationId"),
            "tenantId": payload.get("tenantId"),
            "workflowId": md.get("workflowId"),
            "campaignId": md.get("campaignId"),
            "customerId": md.get("customerId"),
            "callId": call_id,
            "userId": md.get("userId"),
            "transport": "ULTRAVOX_SIP",
        }

    async def process_body(self, body: str, ack: Optional[Callable[[], Awaitable[None]]] = None,
                           receive_count: Optional[int] = None) -> None:
        """Run one TRIGGER_CALL end to end.

        `ack` deletes the SQS message; it is invoked as soon as the SIP dial is
        answered.  Before that, any exception propagates and the message stays
        in the queue (safe to retry — nobody's phone rang to completion).
        After answer, retrying would double-call the person, so the message is
        acked first and later failures only end this call.
        """
        payload = json.loads(body)
        msg = self._parser.parse(payload)

        to_number = msg.primary_phone_number()
        system_prompt = msg.metadata.prompt_text

        profile = self._cfg.resolve_profile(to_number)

        room_name = f"call-{uuid.uuid4().hex[:6]}"
        self._log.info(
            "[SQS] TRIGGER_CALL received id=%s tenantId=%s orgId=%s to=%s room=%s provider=%s phoneCount=%d",
            msg.id,
            msg.tenant_id,
            msg.organization_id,
            to_number,
            room_name,
            profile.provider,
            len(msg.metadata.phone_numbers),
        )

        # Per-call logger: with concurrent calls, every line from this call's
        # RTC/SIP/audio stack must stay attributable in the interleaved log.
        call_log = CallLogAdapter(self._log, {"call_id": msg.id, "room": room_name})

        agent = BridgeAgent(self._cfg, call_log, room_name, profile)
        await agent.connect_livekit()

        # Full payload contains the prompt and customer data — debug only.
        call_log.debug("payload=%s", payload)

        metadata = self.build_ultravox_metadata(payload)
        call_log.info("ultravoxMetadata=%s", metadata)

        # CALL_HISTORY events echo the same tracking ids sent to Ultravox —
        # one id-resolution (build_ultravox_metadata), two consumers.  The
        # base metadata is the call context only this gateway knows: room is
        # the Loki correlation key, toNumber is which of the contact's
        # numbers was actually dialed, country/provider is which SIP leg
        # carried the call.
        emitter = CallHistoryEmitter(self._events, call_log, metadata, base_metadata={
            "room": room_name,
            "toNumber": to_number,
            "country": profile.country_code,
            "provider": profile.provider,
        })

        voice = msg.metadata.voice_id or profile.ultravox_voice
        voice_source = "trigger" if msg.metadata.voice_id else "profile"
        self._log.info(
            "[SQS] creating Ultravox call for id=%s room=%s voice=%s voiceSource=%s",
            msg.id, room_name, voice, voice_source,
        )

        try:
            uv_call = await self._uv.create_ws_call_join_url(
                system_prompt=system_prompt,
                voice=voice,
                metadata=metadata,
                greeting_message=msg.metadata.greeting_message,
                country_code=profile.country_code,
                language_hint=profile.language_hint,
            )
            uv_join_url = uv_call.join_url

            self._log.info(
                "[SQS] dialing SIP id=%s room=%s to=%s (waiting for answer, timeout=%.0fs)",
                msg.id, room_name, to_number, DIAL_ANSWER_TIMEOUT_S,
            )
            await emitter.emit("CALL_ATTEMPT_STARTED", "Dial attempt started")
            dial_started_at = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._dialer.dial_out(room_name, to_number, profile),
                    timeout=DIAL_ANSWER_TIMEOUT_S,
                )
            except asyncio.TimeoutError as e:
                # LiveKit fails an unanswered dial on its own (SIP 408) well
                # before this guard; hitting it means the API hung.  Own
                # category so it never contaminates the no-answer metric.
                raise CallNotAnsweredError("dial-timeout") from e
        except CallNotAnsweredError as e:
            # Unreachable callee (no-answer/busy/declined/...): a business
            # outcome, not an error.  Ack so SQS never redials on a loop —
            # redial policy belongs to the campaign system.
            await agent.teardown()
            self._log.warning(
                "[SQS] call not answered id=%s room=%s to=%s reason=%s — acking, no retry",
                msg.id, room_name, to_number, e.reason,
            )
            # Digicob's return file reads sipStatus from here; the key is
            # omitted when there is no SIP code (dial-timeout) — never invented.
            unreachable: Dict[str, Any] = {"reason": e.reason}
            if e.sip_status is not None:
                unreachable["sipStatus"] = e.sip_status
            await emitter.emit(
                "CALL_NOT_ANSWERED", f"Callee unreachable: {e.reason}", unreachable,
            )
            if ack is not None:
                try:
                    await ack()
                except Exception:
                    self._log.exception(
                        "[SQS] ack failed for unanswered call id=%s room=%s "
                        "(message may be redelivered)", msg.id, room_name,
                    )
            return
        except Exception as e:
            # Genuine system error before anyone was reached (Ultravox REST,
            # trunk auth, network): drop whatever SIP leg LiveKit may have
            # started and let the message be retried (the queue's redrive
            # policy DLQs it after maxReceiveCount attempts).
            await agent.teardown()
            failure: Dict[str, Any] = {"reason": "system-error", "errorType": type(e).__name__}
            # Unmapped SIP failures (e.g. 5xx from the trunk) still carry a
            # code worth surfacing in the Digicob file; omitted when absent.
            failure_sip_status = extract_sip_status(e)
            if failure_sip_status is not None:
                failure["sipStatus"] = failure_sip_status
            if receive_count is not None:
                failure["attempt"] = receive_count
            await emitter.emit(
                "SIP_CALL_FAILED", "System error before answer; message will be retried", failure,
            )
            raise

        self._log.info("[SQS] SIP dial answered id=%s room=%s to=%s", msg.id, room_name, to_number)
        answered_at = time.monotonic()
        await emitter.emit(
            "SIP_DIAL_ANSWERED", "SIP dial answered",
            {"answerDelaySeconds": int(round(answered_at - dial_started_at))},
        )

        # Point of no return: the callee answered.  From here an SQS redelivery
        # would dial the same person again, so ack (delete) the message now;
        # failures beyond this point are logged but never re-queued.
        if ack is not None:
            try:
                await ack()
            except Exception:
                self._log.exception(
                    "[SQS] ack failed after answer id=%s room=%s; continuing the live call "
                    "(message may be redelivered)", msg.id, room_name,
                )

        async def _on_bridge_active() -> None:
            await emitter.emit("SIP_BRIDGE_ACTIVE", "Audio bridge streaming")

        agent.on_bridge_active = _on_bridge_active

        def _call_ended_metadata(end_reason: str) -> Dict[str, Any]:
            # durationSeconds feeds the backend's talk-time (Digicob return
            # and report totals); measured from answer, like durationS.
            md: Dict[str, Any] = {
                "durationSeconds": int(round(time.monotonic() - answered_at)),
                "endReason": end_reason,
            }
            if uv_call.call_id:
                md["ultravoxCallId"] = uv_call.call_id
            return md

        try:
            await agent.run_bridge(uv_join_url, remote_track_timeout=REMOTE_TRACK_TIMEOUT_S)
        except Exception:
            # Post-answer failure: the call happened (talk time is real) and
            # will NOT be retried (already acked), so this is a SIP_CALL_ENDED
            # with an abnormal endReason — not a SIP_CALL_FAILED, which would
            # move the customer to "failed" after a real conversation.
            await emitter.emit(
                "SIP_CALL_ENDED", "Call ended by bridge error", _call_ended_metadata("bridge-error"),
            )
            raise

        end_reason = getattr(agent, "end_reason", None) or "unknown"
        self._log.info(
            "[SQS] audio bridge finished for id=%s room=%s to=%s durationS=%.1f endReason=%s",
            msg.id, room_name, to_number, time.monotonic() - answered_at, end_reason,
        )
        await emitter.emit("SIP_CALL_ENDED", "Call ended", _call_ended_metadata(end_reason))


async def run_worker_loop(cfg: BridgeConfig, log: logging.Logger, consumer, processor) -> None:
    """Poll SQS and run each call as its own task, capped by MAX_CONCURRENT_CALLS.

    A message is only pulled from the queue when there is a free call slot:
    a message sitting in memory waiting for a slot would have its visibility
    clock running, ending in a phantom redelivery.
    """
    in_flight: set = set()

    async def _heartbeat() -> None:
        while True:
            log.info("[HB] alive inFlight=%d max=%d", len(in_flight), cfg.max_concurrent_calls)
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    def _task_done(task: asyncio.Task) -> None:
        in_flight.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # _handle catches everything, so this is a genuine bug surfacing.
            log.error("[SQS] call task crashed unexpectedly: %r", task.exception())
        log.info("[SQS] call task finished inFlight=%d/%d", len(in_flight), cfg.max_concurrent_calls)

    async def _handle(m) -> None:
        async def ack(receipt_handle: str = m.receipt_handle) -> None:
            await asyncio.to_thread(consumer.delete, receipt_handle)
            log.info("[SQS] deleted message receiptHandlePrefix=%s", receipt_handle[:10])

        try:
            receive_count = int(m.attributes.get("ApproximateReceiveCount", 0))
        except (TypeError, ValueError):
            receive_count = 0

        try:
            await processor.process_body(m.body, ack, receive_count=receive_count or None)
        except Exception as e:
            # errorType/receiveCount feed the Grafana failure-by-type panel:
            # ValueError/JSONDecodeError = bad payload (permanent — will DLQ
            # after maxReceiveCount), ConnectionError etc. = transient.
            log.exception(
                "[SQS] message processing failed errorType=%s receiveCount=%d "
                "(unless already acked at answer, it will be retried after "
                "visibility timeout; the redrive policy DLQs it after max receives)",
                type(e).__name__, receive_count,
            )

    hb_task = asyncio.create_task(_heartbeat())
    try:
        while True:
            while len(in_flight) >= cfg.max_concurrent_calls:
                await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)

            try:
                msgs = await asyncio.to_thread(consumer.receive, 1, 20, 300)
            except Exception:
                # A transient network error must never kill the worker: live calls
                # run in their own tasks and keep going; we just retry the poll.
                log.warning(
                    "[SQS] receive failed; retrying in %.0fs", POLL_ERROR_BACKOFF_S, exc_info=True,
                )
                await asyncio.sleep(POLL_ERROR_BACKOFF_S)
                continue

            if not msgs:
                log.debug("[SQS] long poll returned no messages")
                continue

            for m in msgs:
                log.info("[SQS] received message receiptHandlePrefix=%s bodyLength=%d", m.receipt_handle[:10], len(m.body))
                task = asyncio.create_task(_handle(m))
                in_flight.add(task)
                task.add_done_callback(_task_done)
                log.info("[SQS] call task started inFlight=%d/%d", len(in_flight), cfg.max_concurrent_calls)
    finally:
        hb_task.cancel()


async def main() -> None:
    cfg = BridgeConfig()

    cfg.require("ULTRAVOX_API_KEY", cfg.ultravox_api_key)

    # httpx logs every request at INFO ("HTTP Request: POST ..."), which is
    # pure noise here: our own structured lines already cover the Ultravox
    # REST calls, and the Loki pushes would spam stdout on every flush.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Attached to the root logger so every line (worker, bridge, libs) that
    # reaches stdout also reaches Grafana.  Optional: without GRAFANA_* env
    # vars the worker runs stdout-only, exactly as before.
    from .observability import build_loki_handler
    loki = build_loki_handler(cfg)
    if loki is not None:
        logging.getLogger().addHandler(loki)
        log.info("[Obs] Grafana Loki shipping enabled env=%s", cfg.environment)
    else:
        log.info("[Obs] Grafana Loki shipping disabled (GRAFANA_* not set)")

    ConfigDumper(cfg, log).dump_effective_config()

    sqs = SqsClientFactory(cfg).build()
    queue_url = SqsQueueResolver(cfg, log).resolve_queue_url()
    consumer = SqsLongPollConsumer(sqs, queue_url, log)
    event_publisher = build_call_history_publisher(cfg, sqs, log)
    if isinstance(event_publisher, NullCallHistoryPublisher):
        log.info("[Events] CALL_HISTORY publishing disabled (CALL_HISTORY_QUEUE_NAME not set)")
    processor = TriggerCallProcessor(cfg, log, event_publisher)

    log.info(
        "[SQS] starting long polling worker queueUrl=%s maxConcurrentCalls=%d",
        queue_url, cfg.max_concurrent_calls,
    )

    try:
        await run_worker_loop(cfg, log, consumer, processor)
    finally:
        if loki is not None:
            loki.close()  # flush pending log batches before the process exits


if __name__ == "__main__":
    asyncio.run(main())

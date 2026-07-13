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
from .livekit_client import LiveKitSipDialer
from .agent import BridgeAgent

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
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log
        self._parser = TriggerCallMessageParser()
        self._uv = UltravoxCallClient(cfg, log)
        self._dialer = LiveKitSipDialer(log)

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

    async def process_body(self, body: str, ack: Optional[Callable[[], Awaitable[None]]] = None) -> None:
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

        voice = msg.metadata.voice_id or profile.ultravox_voice
        voice_source = "trigger" if msg.metadata.voice_id else "profile"
        self._log.info(
            "[SQS] creating Ultravox call for id=%s room=%s voice=%s voiceSource=%s",
            msg.id, room_name, voice, voice_source,
        )

        try:
            uv_join_url = await self._uv.create_ws_call_join_url(
                system_prompt=system_prompt,
                voice=voice,
                metadata=metadata,
                greeting_message=msg.metadata.greeting_message,
                country_code=profile.country_code,
                language_hint=profile.language_hint,
            )

            self._log.info(
                "[SQS] dialing SIP id=%s room=%s to=%s (waiting for answer, timeout=%.0fs)",
                msg.id, room_name, to_number, DIAL_ANSWER_TIMEOUT_S,
            )
            await asyncio.wait_for(
                self._dialer.dial_out(room_name, to_number, profile),
                timeout=DIAL_ANSWER_TIMEOUT_S,
            )
        except Exception:
            # Nobody was reached yet (Ultravox REST error, trunk rejection, or
            # nobody answered in time): drop whatever SIP leg LiveKit may have
            # started and let the message be retried.
            await agent.teardown()
            raise

        self._log.info("[SQS] SIP dial answered id=%s room=%s to=%s", msg.id, room_name, to_number)
        answered_at = time.monotonic()

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

        await agent.run_bridge(uv_join_url, remote_track_timeout=REMOTE_TRACK_TIMEOUT_S)
        self._log.info(
            "[SQS] audio bridge finished for id=%s room=%s to=%s durationS=%.1f",
            msg.id, room_name, to_number, time.monotonic() - answered_at,
        )


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
            await processor.process_body(m.body, ack)
        except Exception:
            log.exception(
                "[SQS] message processing failed (unless already acked at answer, "
                "it will be retried after visibility timeout)"
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
    processor = TriggerCallProcessor(cfg, log)

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

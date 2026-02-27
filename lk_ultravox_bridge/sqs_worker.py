from __future__ import annotations

import asyncio
import json
import logging
import uuid

from dotenv import load_dotenv

# Load .env before importing BridgeConfig and other modules that depend on environment.
load_dotenv(override=True)

from .config import BridgeConfig
from .logging_utils import ConfigDumper
from .sqs_consumer import SqsClientFactory, SqsQueueResolver, SqsLongPollConsumer
from .message_models import TriggerCallMessageParser
from .ultravox_client import UltravoxCallClient
from .livekit_client import LiveKitSipDialer
from .agent import BridgeAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sqs-worker")


class TriggerCallProcessor:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log
        self._parser = TriggerCallMessageParser()
        self._uv = UltravoxCallClient(cfg, log)
        self._dialer = LiveKitSipDialer(log)

    async def process_body(self, body: str) -> None:
        self._log.debug("[SQS] processing raw message body length=%d", len(body))
        payload = json.loads(body)

        try:
            msg = self._parser.parse(payload)
        except Exception as e:
            self._log.error("[SQS] failed to parse TRIGGER_CALL payload: %s", e)
            raise

        to_number = msg.primary_phone_number()
        system_prompt = msg.metadata.prompt_text

        profile = self._cfg.resolve_profile(to_number)

        room_name = f"call-{uuid.uuid4().hex[:6]}"
        self._log.info(
            "[SQS] TRIGGER_CALL received id=%s tenantId=%s orgId=%s to=%s room=%s country=%s phoneCount=%d",
            msg.id,
            msg.tenant_id,
            msg.organization_id,
            to_number,
            room_name,
            profile.country_code,
            len(msg.metadata.phone_numbers),
        )

        agent = BridgeAgent(self._cfg, self._log, room_name, profile)
        await agent.connect_livekit()

        self._log.info("[SQS] creating Ultravox call for id=%s room=%s", msg.id, room_name)
        uv_join_url = await self._uv.create_ws_call_join_url(system_prompt=system_prompt)

        dial_task = asyncio.create_task(self._dialer.dial_out(room_name, to_number, profile))
        self._log.info("[SQS] LiveKit SIP dial-out started in background id=%s room=%s to=%s", msg.id, room_name, to_number)

        try:
            await agent.run_bridge(uv_join_url)
            self._log.info("[SQS] audio bridge finished for id=%s room=%s to=%s", msg.id, room_name, to_number)
        finally:
            try:
                await dial_task
            except Exception:
                self._log.exception("[SQS] dial task failed for id=%s room=%s to=%s", msg.id, room_name, to_number)


async def main() -> None:
    cfg = BridgeConfig()

    cfg.require("ULTRAVOX_API_KEY", cfg.ultravox_api_key)
    cfg.require("ULTRAVOX_VOICE", cfg.ultravox_voice)

    ConfigDumper(cfg, log).dump_effective_config()

    sqs = SqsClientFactory(cfg).build()
    queue_url = SqsQueueResolver(cfg, log).resolve_queue_url()
    consumer = SqsLongPollConsumer(sqs, queue_url, log)
    processor = TriggerCallProcessor(cfg, log)

    log.info("[SQS] starting long polling worker queueUrl=%s", queue_url)

    while True:
        msgs = await asyncio.to_thread(consumer.receive, 1, 20, 300)
        if not msgs:
            log.debug("[SQS] long poll returned no messages")
            continue

        log.debug("[SQS] long poll returned %d message(s)", len(msgs))

        for m in msgs:
            log.info("[SQS] received message receiptHandlePrefix=%s bodyLength=%d", m.receipt_handle[:10], len(m.body))
            try:
                await processor.process_body(m.body)
                await asyncio.to_thread(consumer.delete, m.receipt_handle)
                log.info("[SQS] deleted message receiptHandlePrefix=%s", m.receipt_handle[:10])
            except Exception:
                log.exception("[SQS] message processing failed; it will be retried after visibility timeout")


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import asyncio
import json
import logging
import uuid

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
        self._dialer = LiveKitSipDialer(cfg, log)

    async def process_body(self, body: str) -> None:
        payload = json.loads(body)
        msg = self._parser.parse(payload)

        to_number = msg.primary_phone_number()
        system_prompt = msg.metadata.prompt_text

        room_name = f"call-{uuid.uuid4().hex[:6]}"
        self._log.info("[SQS] TRIGGER_CALL id=%s tenantId=%s orgId=%s to=%s room=%s",
                       msg.id, msg.tenant_id, msg.organization_id, to_number, room_name)

        agent = BridgeAgent(self._cfg, self._log, room_name)
        await agent.connect_livekit()

        uv_join_url = await self._uv.create_ws_call_join_url(system_prompt=system_prompt)

        dial_task = asyncio.create_task(self._dialer.dial_out(room_name, to_number))
        self._log.info("[SQS] dialing started in background")

        try:
            await agent.run_bridge(uv_join_url)
        finally:
            try:
                await dial_task
            except Exception as e:
                self._log.exception("[SQS] dial task failed: %r", e)


async def main() -> None:
    cfg = BridgeConfig()

    cfg.require("LIVEKIT_API_KEY", cfg.livekit_api_key)
    cfg.require("LIVEKIT_API_SECRET", cfg.livekit_api_secret)
    cfg.require("ULTRAVOX_API_KEY", cfg.ultravox_api_key)
    cfg.require("ULTRAVOX_VOICE", cfg.ultravox_voice)

    ConfigDumper(cfg, log).dump_effective_config()

    sqs = SqsClientFactory(cfg).build()
    queue_url = SqsQueueResolver(cfg, log).resolve_queue_url()
    consumer = SqsLongPollConsumer(sqs, queue_url, log)
    processor = TriggerCallProcessor(cfg, log)

    log.info("[SQS] long polling queueUrl=%s", queue_url)

    while True:
        msgs = await asyncio.to_thread(consumer.receive, 1, 20, 300)
        if not msgs:
            continue

        for m in msgs:
            try:
                await processor.process_body(m.body)
                await asyncio.to_thread(consumer.delete, m.receipt_handle)
                log.info("[SQS] deleted message")
            except Exception as e:
                log.exception("[SQS] processing failed (message will return after visibility timeout): %r", e)


if __name__ == "__main__":
    asyncio.run(main())

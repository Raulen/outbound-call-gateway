"""CALL_HISTORY event publishing (CallHistoryQueue).

Every meaningful transition of an outbound call is published to the backend's
CallHistoryQueue as a CALL_HISTORY message (raw JSON body, no SNS envelope).
The consumer persists each message as an append-only call_history row and
moves the customer status through its own de-para.

Sample messages for every status this module can emit live in
``event_samples/`` at the project root; ``tests/unit/test_call_history.py``
fails if the samples and this module ever drift apart.

Design constraint (same as observability.LokiShipper): this process moves
live phone audio — publishing is best-effort and must never raise into, or
block, the call flow.  A lost event is always preferable to a degraded call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import BridgeConfig
from .observability import APP_NAME

MESSAGE_TYPE = "CALL_HISTORY"

# Statuses this gateway emits (subset of the consumer's CallHistoryStatus
# enum, plus CALL_NOT_ANSWERED which is new — the consumer persists unknown
# statuses with a warn until its de-para learns it).  Keep in sync with
# event_samples/: the drift test asserts sample files == this set.
EMITTED_STATUSES = frozenset({
    "CALL_ATTEMPT_STARTED",  # SIP dial-out is being created (phone about to ring)
    "SIP_DIAL_ANSWERED",     # callee answered (ack-at-answer point)
    "SIP_BRIDGE_ACTIVE",     # audio bridge streaming (agent and callee connected)
    "SIP_CALL_ENDED",        # call over; metadataJson carries durationSeconds + endReason
    "CALL_NOT_ANSWERED",     # unreachable callee; metadataJson carries reason + sipStatus
    "SIP_CALL_FAILED",       # system error before answer; message will be retried
})


def uuid7() -> str:
    """RFC 9562 UUIDv7 (time-ordered): 48-bit unix-ms timestamp + random.

    stdlib uuid.uuid7() only exists from Python 3.14; this project runs 3.10+.
    """
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts_ms & 0xFFFF_FFFF_FFFF) << 80          # 48-bit timestamp
    value |= 0x7 << 76                                # version 7
    value |= ((rand >> 68) & 0x0FFF) << 64            # rand_a (12 bits)
    value |= 0x2 << 62                                # variant 10
    value |= rand & 0x3FFF_FFFF_FFFF_FFFF             # rand_b (62 bits)
    return str(uuid.UUID(int=value))


class NullCallHistoryPublisher:
    """Used when CALL_HISTORY_QUEUE_NAME is unset: events disabled."""

    async def publish(self, body: Dict[str, Any]) -> None:
        pass


class SqsCallHistoryPublisher:
    def __init__(self, sqs_client, queue_url: str, log: logging.Logger):
        self._client = sqs_client
        self._queue_url = queue_url
        self._log = log

    async def publish(self, body: Dict[str, Any]) -> None:
        # boto3 is sync; to_thread keeps the event loop (and the audio it
        # drives) unblocked.  Failures are logged and swallowed: best-effort.
        try:
            await asyncio.to_thread(
                self._client.send_message,
                QueueUrl=self._queue_url,
                MessageBody=json.dumps(body),
            )
        except Exception:
            self._log.warning(
                "[Events] CALL_HISTORY publish failed status=%s callId=%s",
                body.get("metadata", {}).get("status"),
                body.get("metadata", {}).get("callId"),
                exc_info=True,
            )


def build_call_history_publisher(cfg: BridgeConfig, sqs_client, log: logging.Logger):
    """SqsCallHistoryPublisher when configured, Null publisher otherwise."""
    if not cfg.call_history_queue_name:
        return NullCallHistoryPublisher()
    queue_url = (
        f"https://sqs.{cfg.aws_region}.amazonaws.com/"
        f"{cfg.aws_account_id}/{cfg.call_history_queue_name}"
    )
    log.info("[Events] CALL_HISTORY publishing enabled queueUrl=%s", queue_url)
    return SqsCallHistoryPublisher(sqs_client, queue_url, log)


class CallHistoryEmitter:
    """Per-call emitter: holds the correlation ids echoed from TRIGGER_CALL
    (the source of truth for the backend) and stamps each status message.
    """

    def __init__(self, publisher, log: logging.Logger, tracking: Dict[str, Any],
                 base_metadata: Optional[Dict[str, Any]] = None):
        self._publisher = publisher
        self._log = log
        self._tracking = tracking
        # Merged into every event's metadataJson: call context only this
        # gateway knows (room for Loki correlation, the number actually
        # dialed, which country/provider leg carried the call).
        self._base_metadata = dict(base_metadata or {})
        # The consumer discards messages with blank callId/customerId; warn
        # here so the gap is visible on our side instead of silently dropped.
        for required in ("callId", "customerId"):
            if not (tracking.get(required) or ""):
                log.warning("[Events] TRIGGER_CALL has blank %s; consumer will discard CALL_HISTORY", required)

    def _build(self, status: str, description: str, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        t = self._tracking
        return {
            "id": uuid7(),
            "messageType": MESSAGE_TYPE,
            "source": APP_NAME,
            "organizationId": str(t.get("organizationId") or ""),
            "tenantId": str(t.get("tenantId") or ""),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "metadata": {
                "workflowId": str(t.get("workflowId") or ""),
                "campaignId": str(t.get("campaignId") or ""),
                "customerId": str(t.get("customerId") or ""),
                "userId": str(t.get("userId") or ""),
                "callId": str(t.get("callId") or ""),
                "status": status,
                "statusDescription": description,
                # Contract: a JSON-serialized *string*, not an object.
                "metadataJson": json.dumps({**self._base_metadata, **(metadata or {})}),
            },
        }

    async def emit(self, status: str, description: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        assert status in EMITTED_STATUSES, f"unknown CALL_HISTORY status: {status}"
        try:
            body = self._build(status, description, metadata)
            self._log.info("[Events] CALL_HISTORY status=%s callId=%s", status, body["metadata"]["callId"])
            await self._publisher.publish(body)
        except Exception:
            # Emission must never break a live call.
            self._log.warning("[Events] CALL_HISTORY emit failed status=%s", status, exc_info=True)

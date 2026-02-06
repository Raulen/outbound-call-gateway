from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

import boto3

from .config import BridgeConfig


@dataclass(frozen=True)
class SqsMessage:
    receipt_handle: str
    body: str
    attributes: Dict[str, Any]


class SqsClientFactory:
    def __init__(self, cfg: BridgeConfig):
        self._cfg = cfg

    def build(self):
        if (self._cfg.aws_access_key_id and self._cfg.aws_access_key_id != "none"
                and self._cfg.aws_secret_access_key and self._cfg.aws_secret_access_key != "none"):
            session = boto3.Session(
                aws_access_key_id=self._cfg.aws_access_key_id,
                aws_secret_access_key=self._cfg.aws_secret_access_key,
                region_name=self._cfg.aws_region,
            )
        else:
            session = boto3.Session(profile_name=self._cfg.aws_profile, region_name=self._cfg.aws_region)

        return session.client("sqs")


class SqsQueueResolver:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log

    def resolve_queue_url(self) -> str:
        return f"https://sqs.{self._cfg.aws_region}.amazonaws.com/{self._cfg.aws_account_id}/{self._cfg.sqs_queue_name}"


class SqsLongPollConsumer:
    def __init__(self, client, queue_url: str, log: logging.Logger):
        self._client = client
        self._queue_url = queue_url
        self._log = log

    def receive(self, max_messages: int = 1, wait_seconds: int = 20, visibility_timeout: int = 120) -> List[SqsMessage]:
        resp = self._client.receive_message(
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=visibility_timeout,
        )
        msgs: List[SqsMessage] = []
        for m in resp.get("Messages", []):
            msgs.append(SqsMessage(
                receipt_handle=m["ReceiptHandle"],
                body=m.get("Body", ""),
                attributes=m.get("Attributes", {}),
            ))
        return msgs

    def delete(self, receipt_handle: str) -> None:
        self._client.delete_message(QueueUrl=self._queue_url, ReceiptHandle=receipt_handle)

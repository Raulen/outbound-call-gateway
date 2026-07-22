"""SQS client factory, queue URL resolution and long-poll consumer mapping."""
from __future__ import annotations

import logging

import boto3
import pytest

from lk_ultravox_bridge.sqs_consumer import (
    SqsClientFactory,
    SqsLongPollConsumer,
    SqsMessage,
    SqsQueueResolver,
)

from tests.conftest import make_config

log = logging.getLogger("test")


class FakeBotoSession:
    """Records how boto3.Session was constructed."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        FakeBotoSession.last_kwargs = kwargs

    def client(self, service_name):
        assert service_name == "sqs"
        return "fake-sqs-client"


@pytest.fixture
def fake_session(monkeypatch):
    monkeypatch.setattr(boto3, "Session", FakeBotoSession)
    FakeBotoSession.last_kwargs = {}
    return FakeBotoSession


class TestSqsClientFactory:
    def test_static_keys_when_both_set(self, fake_session):
        cfg = make_config(aws_access_key_id="AKIATEST", aws_secret_access_key="secret123")
        SqsClientFactory(cfg).build()
        assert fake_session.last_kwargs == {
            "aws_access_key_id": "AKIATEST",
            "aws_secret_access_key": "secret123",
            "region_name": "us-east-1",
        }

    def test_profile_when_keys_empty(self, fake_session):
        cfg = make_config(aws_access_key_id="", aws_secret_access_key="")
        SqsClientFactory(cfg).build()
        assert fake_session.last_kwargs == {
            "profile_name": "test-profile",
            "region_name": "us-east-1",
        }

    @pytest.mark.parametrize(
        "access,secret",
        [("none", "secret123"), ("AKIATEST", "none"), ("none", "none")],
    )
    def test_none_sentinel_falls_back_to_profile(self, fake_session, access, secret):
        # "none" (literal string) is the documented way to disable static keys
        # in environments where the var must exist but should be ignored.
        cfg = make_config(aws_access_key_id=access, aws_secret_access_key=secret)
        SqsClientFactory(cfg).build()
        assert "profile_name" in fake_session.last_kwargs
        assert "aws_access_key_id" not in fake_session.last_kwargs


class TestSqsQueueResolver:
    def test_url_built_from_region_account_and_queue(self):
        cfg = make_config(aws_region="sa-east-1", aws_account_id="111122223333", sqs_queue_name="MyQueue")
        url = SqsQueueResolver(cfg, log).resolve_queue_url()
        assert url == "https://sqs.sa-east-1.amazonaws.com/111122223333/MyQueue"


class FakeSqsClient:
    def __init__(self, response=None):
        self.response = response or {}
        self.receive_calls = []
        self.delete_calls = []

    def receive_message(self, **kwargs):
        self.receive_calls.append(kwargs)
        return self.response

    def delete_message(self, **kwargs):
        self.delete_calls.append(kwargs)


class TestSqsLongPollConsumer:
    QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123/TestQueue"

    def test_receive_passes_polling_parameters(self):
        client = FakeSqsClient()
        consumer = SqsLongPollConsumer(client, self.QUEUE_URL, log)
        consumer.receive(max_messages=1, wait_seconds=20, visibility_timeout=300)
        assert client.receive_calls == [{
            "QueueUrl": self.QUEUE_URL,
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 20,
            "VisibilityTimeout": 300,
            # Delivery-attempt number: feeds the CALL_FAILED `attempt` field
            # and the DLQ-bound Grafana stat (redrive at maxReceiveCount=5).
            "AttributeNames": ["ApproximateReceiveCount"],
        }]

    def test_receive_maps_messages(self):
        client = FakeSqsClient(response={
            "Messages": [
                {"ReceiptHandle": "rh-1", "Body": '{"a":1}', "Attributes": {"SentTimestamp": "1"}},
                {"ReceiptHandle": "rh-2"},  # Body/Attributes may be absent
            ]
        })
        consumer = SqsLongPollConsumer(client, self.QUEUE_URL, log)
        msgs = consumer.receive()
        assert msgs == [
            SqsMessage(receipt_handle="rh-1", body='{"a":1}', attributes={"SentTimestamp": "1"}),
            SqsMessage(receipt_handle="rh-2", body="", attributes={}),
        ]

    def test_empty_poll_returns_empty_list(self):
        consumer = SqsLongPollConsumer(FakeSqsClient(), self.QUEUE_URL, log)
        assert consumer.receive() == []

    def test_delete_targets_queue_and_receipt(self):
        client = FakeSqsClient()
        consumer = SqsLongPollConsumer(client, self.QUEUE_URL, log)
        consumer.delete("rh-42")
        assert client.delete_calls == [{"QueueUrl": self.QUEUE_URL, "ReceiptHandle": "rh-42"}]

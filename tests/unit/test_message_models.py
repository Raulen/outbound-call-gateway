"""Contract tests for the SQS TRIGGER_CALL message.

A parsing regression here has two failure modes in production:
- a ValueError not raised → we dial a wrong/garbage number;
- a ValueError raised for a valid message → the message loops in the
  queue forever (the worker never deletes on error).
"""
from __future__ import annotations

import pytest

from lk_ultravox_bridge.message_models import (
    PhoneNumber,
    TriggerCallMessage,
    TriggerCallMessageParser,
    TriggerCallMetadata,
)


def valid_payload(**overrides) -> dict:
    """The canonical TRIGGER_CALL payload, as produced by the telephony backend."""
    payload = {
        "id": "msg-001",
        "messageType": "TRIGGER_CALL",
        "source": "campaign-engine",
        "organizationId": "org-1",
        "tenantId": "tenant-1",
        "createdAt": "2026-07-13T12:00:00Z",
        "metadata": {
            "workflowId": "wf-1",
            "campaignId": "cmp-1",
            "customerId": "cust-1",
            "userId": "user-1",
            "telephonyProvider": "twilio",
            "externalCustomerId": "ext-1",
            "fullName": "Maria Silva",
            "direction": "OUTBOUND",
            "phoneNumbers": [{"number": "5511999998888", "order": 1}],
            "subject": {
                "prompt": {
                    "text": "You are a collections agent.",
                    "greetingMessage": "Olá!",
                }
            },
        },
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def parser() -> TriggerCallMessageParser:
    return TriggerCallMessageParser()


class TestParseValidMessage:
    def test_full_message_maps_every_field(self, parser):
        msg = parser.parse(valid_payload())

        assert msg.id == "msg-001"
        assert msg.message_type == "TRIGGER_CALL"
        assert msg.source == "campaign-engine"
        assert msg.organization_id == "org-1"
        assert msg.tenant_id == "tenant-1"
        assert msg.created_at == "2026-07-13T12:00:00Z"

        md = msg.metadata
        assert md.workflow_id == "wf-1"
        assert md.campaign_id == "cmp-1"
        assert md.customer_id == "cust-1"
        assert md.user_id == "user-1"
        assert md.telephony_provider == "twilio"
        assert md.external_customer_id == "ext-1"
        assert md.full_name == "Maria Silva"
        assert md.direction == "OUTBOUND"
        assert md.phone_numbers == [PhoneNumber(number="5511999998888", order=1)]
        assert md.prompt_text == "You are a collections agent."
        assert md.greeting_message == "Olá!"

    def test_numeric_ids_are_coerced_to_str(self, parser):
        msg = parser.parse(valid_payload(id=42, organizationId=7))
        assert msg.id == "42"
        assert msg.organization_id == "7"

    def test_missing_optional_fields_become_empty_or_none(self, parser):
        payload = valid_payload()
        payload["metadata"].pop("externalCustomerId")
        payload["metadata"].pop("fullName")
        payload["metadata"].pop("workflowId")
        payload["metadata"]["subject"]["prompt"].pop("greetingMessage")
        payload.pop("source")

        msg = parser.parse(payload)
        assert msg.source == ""
        assert msg.metadata.external_customer_id is None
        assert msg.metadata.full_name is None
        assert msg.metadata.workflow_id == ""
        assert msg.metadata.greeting_message is None

    def test_voice_id_present_is_mapped(self, parser):
        payload = valid_payload()
        payload["metadata"]["voiceId"] = "voice-custom-1"
        assert parser.parse(payload).metadata.voice_id == "voice-custom-1"

    def test_voice_id_absent_is_none(self, parser):
        assert parser.parse(valid_payload()).metadata.voice_id is None


class TestParseRejection:
    """Every rejection here means the worker will NOT delete the message."""

    def test_wrong_message_type_is_rejected(self, parser):
        with pytest.raises(ValueError, match="messageType"):
            parser.parse(valid_payload(messageType="CALL_ENDED"))

    def test_missing_message_type_is_rejected(self, parser):
        payload = valid_payload()
        del payload["messageType"]
        with pytest.raises(ValueError, match="messageType"):
            parser.parse(payload)

    def test_missing_prompt_text_is_rejected(self, parser):
        payload = valid_payload()
        del payload["metadata"]["subject"]["prompt"]["text"]
        with pytest.raises(ValueError, match="prompt.text"):
            parser.parse(payload)

    def test_empty_prompt_text_is_rejected(self, parser):
        payload = valid_payload()
        payload["metadata"]["subject"]["prompt"]["text"] = ""
        with pytest.raises(ValueError, match="prompt.text"):
            parser.parse(payload)

    def test_missing_subject_is_rejected(self, parser):
        payload = valid_payload()
        del payload["metadata"]["subject"]
        with pytest.raises(ValueError, match="prompt.text"):
            parser.parse(payload)

    def test_missing_metadata_entirely_is_rejected(self, parser):
        payload = valid_payload()
        del payload["metadata"]
        with pytest.raises(ValueError, match="prompt.text"):
            parser.parse(payload)


class TestPhoneNumbers:
    def test_primary_is_lowest_order_not_list_position(self, parser):
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = [
            {"number": "5511000000002", "order": 2},
            {"number": "5511000000001", "order": 1},
            {"number": "5511000000003", "order": 3},
        ]
        msg = parser.parse(payload)
        assert msg.primary_phone_number() == "+5511000000001"

    def test_primary_prefixes_plus(self, parser):
        assert parser.parse(valid_payload()).primary_phone_number() == "+5511999998888"

    def test_empty_phone_list_raises_on_primary(self, parser):
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = []
        msg = parser.parse(payload)
        with pytest.raises(ValueError, match="phoneNumbers is empty"):
            msg.primary_phone_number()

    def test_entries_without_number_are_skipped(self, parser):
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = [
            {"order": 1},
            {"number": "", "order": 2},
            {"number": "5511000000009", "order": 3},
        ]
        msg = parser.parse(payload)
        assert msg.primary_phone_number() == "+5511000000009"

    def test_missing_order_defaults_to_zero(self, parser):
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = [
            {"number": "5511000000002", "order": 1},
            {"number": "5511000000001"},  # no order -> 0, wins
        ]
        msg = parser.parse(payload)
        assert msg.primary_phone_number() == "+5511000000001"

    def test_numeric_number_is_coerced_to_str(self, parser):
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = [{"number": 5511000000007, "order": 1}]
        msg = parser.parse(payload)
        assert msg.primary_phone_number() == "+5511000000007"

    def test_number_already_with_plus_gets_double_plus_KNOWN_BUG(self, parser):
        # Documents current behavior: primary_phone_number() blindly prepends
        # "+".  If the producer ever sends E.164 numbers WITH the "+", the
        # dial target becomes "++55..." and resolve_profile/SIP dial will
        # misbehave.  If this test starts failing because the code now
        # normalizes the prefix, that is an improvement: update this test.
        payload = valid_payload()
        payload["metadata"]["phoneNumbers"] = [{"number": "+5511999998888", "order": 1}]
        msg = parser.parse(payload)
        assert msg.primary_phone_number() == "++5511999998888"

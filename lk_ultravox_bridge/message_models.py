from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PhoneNumber:
    number: str
    order: int


@dataclass(frozen=True)
class TriggerCallMetadata:
    workflow_id: str
    campaign_id: str
    customer_id: str
    user_id: str
    telephony_provider: str
    external_customer_id: Optional[str]
    full_name: Optional[str]
    direction: str
    phone_numbers: List[PhoneNumber]
    prompt_text: str
    greeting_message: Optional[str]


@dataclass(frozen=True)
class TriggerCallMessage:
    id: str
    message_type: str
    source: str
    organization_id: str
    tenant_id: str
    created_at: str
    metadata: TriggerCallMetadata

    def primary_phone_number(self) -> str:
        nums = sorted(self.metadata.phone_numbers, key=lambda p: p.order)
        if not nums:
            raise ValueError("metadata.phoneNumbers is empty")
        return "+"+nums[0].number


class TriggerCallMessageParser:
    def parse(self, payload: Dict[str, Any]) -> TriggerCallMessage:
        mt = payload.get("messageType")
        if mt != "TRIGGER_CALL":
            raise ValueError(f"Unsupported messageType={mt!r}")

        md = payload.get("metadata") or {}
        subject = md.get("subject") or {}
        prompt = (subject.get("prompt") or {})
        prompt_text = prompt.get("text")
        if not prompt_text:
            raise ValueError("metadata.subject.prompt.text is required")

        phones_raw = md.get("phoneNumbers") or []
        phone_numbers: List[PhoneNumber] = []
        for item in phones_raw:
            n = item.get("number")
            o = item.get("order", 0)
            if n:
                phone_numbers.append(PhoneNumber(number=str(n), order=int(o)))

        meta = TriggerCallMetadata(
            workflow_id=str(md.get("workflowId") or ""),
            campaign_id=str(md.get("campaignId") or ""),
            customer_id=str(md.get("customerId") or ""),
            user_id=str(md.get("userId") or ""),
            telephony_provider=str(md.get("telephonyProvider") or ""),
            external_customer_id=(md.get("externalCustomerId")),
            full_name=(md.get("fullName")),
            direction=str(md.get("direction") or ""),
            phone_numbers=phone_numbers,
            prompt_text=str(prompt_text),
            greeting_message=(prompt.get("greetingMessage")),
        )

        return TriggerCallMessage(
            id=str(payload.get("id") or ""),
            message_type=str(payload.get("messageType") or ""),
            source=str(payload.get("source") or ""),
            organization_id=str(payload.get("organizationId") or ""),
            tenant_id=str(payload.get("tenantId") or ""),
            created_at=str(payload.get("createdAt") or ""),
            metadata=meta,
        )

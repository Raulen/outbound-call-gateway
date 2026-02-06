from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeConfig:
    livekit_url: str = os.environ.get("LIVEKIT_URL", "https://switch-h431qm2q.livekit.cloud")
    livekit_wss_url: str = os.environ.get("LIVEKIT_WSS_URL", "wss://switch-h431qm2q.livekit.cloud")
    livekit_api_key: str = os.environ.get("LIVEKIT_API_KEY", "API3NReVEX6jewa")
    livekit_api_secret: str = os.environ.get("LIVEKIT_API_SECRET", "rhg3Yn7F2tOgzt0VmnefdGAiJkeIvixChb5XqQHATyHA")

    sip_trunk_id: str = os.environ.get("SIP_TRUNK_ID", "ST_e27J5bHJFJsK")
    sip_from_number: str = os.environ.get("SIP_FROM_NUMBER", "6007700072")

    ultravox_api_key: str = os.environ.get("ULTRAVOX_API_KEY", "uYvSOOnI.3SWSG5RfhzffFFAg84s2Lga99LGJ6NmQ")
    ultravox_calls_url: str = os.environ.get("ULTRAVOX_CALLS_URL", "https://api.ultravox.ai/api/calls")
    ultravox_voice: str = os.environ.get("ULTRAVOX_VOICE", "2890505a-86fa-40dd-8c31-a7a836fc8427")
    ultravox_system_prompt: str = os.environ.get("ULTRAVOX_SYSTEM_PROMPT", "You are a helpful assistant.")

    sample_rate: int = int(os.environ.get("SAMPLE_RATE", "48000"))
    channels: int = int(os.environ.get("CHANNELS", "1"))
    frame_ms: int = int(os.environ.get("FRAME_MS", "20"))

    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    aws_profile: str = os.environ.get("AWS_PROFILE", "riachuelo-stage")
    aws_access_key_id: str = os.environ.get("AWS_ACCESS_KEY_ID", "AKIAXANWPCZJTUVTNFIY")
    aws_secret_access_key: str = os.environ.get("AWS_SECRET_ACCESS_KEY", "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")
    aws_account_id: str = os.environ.get("AWS_ACCOUNT_ID", "481955878483")
    sqs_queue_name: str = os.environ.get("SQS_QUEUE_NAME", "TriggerCallQueue")

    def require(self, name: str, val: str) -> None:
        if not val:
            raise SystemExit(f"Missing required env var: {name}")

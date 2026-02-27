from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Ensure .env is loaded before reading environment variables for defaults.
# override=True so the .env file is the single source of truth when present.
load_dotenv(override=True)


@dataclass(frozen=True)
class CountryProfile:
    """Per-country LiveKit project + SIP trunk configuration."""

    country_code: str    # e.g. "BR" or "CL"
    prefix: str          # e.g. "+55" or "+56"
    livekit_url: str
    livekit_wss_url: str
    livekit_api_key: str
    livekit_api_secret: str
    sip_trunk_id: str
    sip_from_number: str

    def validate(self) -> None:
        for attr in ("livekit_url", "livekit_wss_url", "livekit_api_key",
                     "livekit_api_secret", "sip_trunk_id", "sip_from_number"):
            if not getattr(self, attr):
                env_key = f"{attr.upper()}_{self.country_code}"
                raise SystemExit(f"Missing required env var for {self.country_code}: {env_key}")


def _build_profile(country_code: str, prefix: str) -> CountryProfile:
    cc = country_code
    return CountryProfile(
        country_code=cc,
        prefix=prefix,
        livekit_url=os.environ.get(f"LIVEKIT_URL_{cc}", ""),
        livekit_wss_url=os.environ.get(f"LIVEKIT_WSS_URL_{cc}", ""),
        livekit_api_key=os.environ.get(f"LIVEKIT_API_KEY_{cc}", ""),
        livekit_api_secret=os.environ.get(f"LIVEKIT_API_SECRET_{cc}", ""),
        sip_trunk_id=os.environ.get(f"SIP_TRUNK_ID_{cc}", ""),
        sip_from_number=os.environ.get(f"SIP_FROM_NUMBER_{cc}", ""),
    )


# Built once at module load (after load_dotenv).
_PROFILE_MAP: dict[str, CountryProfile] = {
    "+55": _build_profile("BR", "+55"),
    "+56": _build_profile("CL", "+56"),
}


@dataclass(frozen=True)
class BridgeConfig:
    ultravox_api_key: str = os.environ.get("ULTRAVOX_API_KEY", "")
    ultravox_calls_url: str = os.environ.get("ULTRAVOX_CALLS_URL", "https://api.ultravox.ai/api/calls")
    ultravox_voice: str = os.environ.get("ULTRAVOX_VOICE", "")
    ultravox_system_prompt: str = os.environ.get("ULTRAVOX_SYSTEM_PROMPT", "You are a helpful assistant.")

    sample_rate: int = int(os.environ.get("SAMPLE_RATE", "48000"))
    channels: int = int(os.environ.get("CHANNELS", "1"))
    frame_ms: int = int(os.environ.get("FRAME_MS", "20"))

    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    aws_profile: str = os.environ.get("AWS_PROFILE", "")
    aws_access_key_id: str = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_account_id: str = os.environ.get("AWS_ACCOUNT_ID", "")
    sqs_queue_name: str = os.environ.get("SQS_QUEUE_NAME", "TriggerCallQueue")

    def require(self, name: str, val: str) -> None:
        if not val:
            raise SystemExit(f"Missing required env var: {name}")

    def resolve_profile(self, to_number: str) -> CountryProfile:
        for prefix, profile in _PROFILE_MAP.items():
            if to_number.startswith(prefix):
                profile.validate()
                return profile
        raise ValueError(f"[SIP] No profile configured for number prefix: {to_number[:5]}...")

    @property
    def profiles(self) -> dict[str, CountryProfile]:
        return _PROFILE_MAP

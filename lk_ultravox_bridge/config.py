from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Ensure .env is loaded before reading environment variables for defaults.
# override=True so the .env file is the single source of truth when present.
load_dotenv(override=True)


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class CountryProfile:
    """Per-country LiveKit project + SIP trunk configuration."""

    country_code: str    # e.g. "BR" or "CL"
    prefix: str          # e.g. "+55" or "+56"
    provider: str        # e.g. "twilio" or "switch"
    livekit_url: str
    livekit_wss_url: str
    livekit_api_key: str
    livekit_api_secret: str
    sip_trunk_id: str
    sip_from_number: str
    ultravox_voice: str  # e.g. ULTRAVOX_VOICE_BR or ULTRAVOX_VOICE_CL
    # BCP47 hint sent to Ultravox to guide ASR and TTS (e.g. "pt-BR").
    # Empty = omit the field from the API payload (Ultravox auto-detects).
    language_hint: str = ""

    def validate(self) -> None:
        for attr in ("livekit_url", "livekit_wss_url", "livekit_api_key",
                     "livekit_api_secret", "sip_trunk_id", "sip_from_number",
                     "ultravox_voice"):
            if not getattr(self, attr):
                env_key = f"{attr.upper()}_{self.country_code}"
                raise SystemExit(f"Missing required env var for {self.country_code}: {env_key}")


def _build_profile(country_code: str, prefix: str, provider: str,
                   default_language_hint: str = "") -> CountryProfile:
    cc = country_code
    # Per-country voice takes priority; falls back to the global ULTRAVOX_VOICE.
    voice = os.environ.get(f"ULTRAVOX_VOICE_{cc}") or os.environ.get("ULTRAVOX_VOICE", "")
    # LANGUAGE_HINT_{CC} overrides the code default; set it to "" to stop
    # sending the hint (no-deploy rollback switch).
    language_hint = os.environ.get(f"LANGUAGE_HINT_{cc}", default_language_hint)
    return CountryProfile(
        country_code=cc,
        prefix=prefix,
        provider=provider,
        livekit_url=os.environ.get(f"LIVEKIT_URL_{cc}", ""),
        livekit_wss_url=os.environ.get(f"LIVEKIT_WSS_URL_{cc}", ""),
        livekit_api_key=os.environ.get(f"LIVEKIT_API_KEY_{cc}", ""),
        livekit_api_secret=os.environ.get(f"LIVEKIT_API_SECRET_{cc}", ""),
        sip_trunk_id=os.environ.get(f"SIP_TRUNK_ID_{cc}", ""),
        sip_from_number=os.environ.get(f"SIP_FROM_NUMBER_{cc}", ""),
        ultravox_voice=voice,
        language_hint=language_hint,
    )


# Built once at module load (after load_dotenv).
_PROFILE_MAP: dict[str, CountryProfile] = {
    "+55": _build_profile("BR", "+55", "twilio", default_language_hint="pt-BR"),
    "+56": _build_profile("CL", "+56", "switch", default_language_hint="es-CL"),
}


@dataclass(frozen=True)
class BridgeConfig:
    ultravox_api_key: str = os.environ.get("ULTRAVOX_API_KEY", "")
    ultravox_calls_url: str = os.environ.get("ULTRAVOX_CALLS_URL", "https://api.ultravox.ai/api/calls")
    ultravox_voice: str = os.environ.get("ULTRAVOX_VOICE", "")
    ultravox_system_prompt: str = os.environ.get("ULTRAVOX_SYSTEM_PROMPT", "You are a helpful assistant.")
    # Ultravox API default is 0 (deterministic), which tends to sound robotic
    # and repetitive on voice calls.  Valid range: 0-1.
    ultravox_temperature: float = float(os.environ.get("ULTRAVOX_TEMPERATURE", "0.3"))
    # Empty = let the API pick its current default model.  Set explicitly to
    # pin a model version across Ultravox default rollouts.
    ultravox_model: str = os.environ.get("ULTRAVOX_MODEL", "")
    # joinTimeout runs from call *creation*, not from SIP answer.  The default
    # (30s) can expire the joinUrl while the callee's phone is still ringing,
    # since we create the Ultravox call before dialing out.
    ultravox_join_timeout: str = os.environ.get("ULTRAVOX_JOIN_TIMEOUT", "60s")
    # How long the agent waits for the callee to speak after pickup before
    # greeting first (firstSpeakerSettings.user.fallback.delay).
    ultravox_greeting_delay: str = os.environ.get("ULTRAVOX_GREETING_DELAY", "4s")
    # Twilio Elastic SIP Trunking has no AMD, so "answered" includes voicemail.
    # When enabled, the Ultravox agent itself detects voicemail (via a system
    # prompt instruction) and ends the call with the built-in hangUp tool.
    # Prompt-based detection is not telecom-grade AMD; disable here if the
    # false-positive rate ever hurts more than talking to voicemail does.
    ultravox_voicemail_hangup: bool = _env_flag("ULTRAVOX_VOICEMAIL_HANGUP", "1")

    sample_rate: int = int(os.environ.get("SAMPLE_RATE", "48000"))
    channels: int = int(os.environ.get("CHANNELS", "1"))
    frame_ms: int = int(os.environ.get("FRAME_MS", "20"))

    # Jitter buffer thresholds (in number of frames).
    # When the receive buffer exceeds max_buffer_frames, old audio is discarded
    # to prevent accumulative playback delay caused by network bursts or
    # backpressure stalls.  Only the most recent keep_buffer_frames are retained.
    max_buffer_frames: int = int(os.environ.get("MAX_BUFFER_FRAMES", "5"))   # 100ms at 20ms/frame
    keep_buffer_frames: int = int(os.environ.get("KEEP_BUFFER_FRAMES", "2")) # 40ms at 20ms/frame

    # Maximum simultaneous calls the SQS worker may run.  1 = strictly serial
    # (the pre-parallelism behavior, always a safe rollback).  Each live call
    # streams 20ms audio frames continuously, so raise this gradually while
    # watching CPU.
    max_concurrent_calls: int = int(os.environ.get("MAX_CONCURRENT_CALLS", "3"))

    # Observability (Grafana Cloud Loki).  All optional: when unset, the
    # worker logs to stdout only, exactly as before.  Metrics are derived
    # from these logs via LogQL — no separate metrics pipeline at this scale
    # (GRAFANA_PROM_* in .env is reserved for when that changes).
    environment: str = os.environ.get("ENVIRONMENT", "dev")
    grafana_loki_url: str = os.environ.get("GRAFANA_LOKI_URL", "")
    grafana_loki_user: str = os.environ.get("GRAFANA_LOKI_USER", "")
    grafana_token: str = os.environ.get("GRAFANA_TOKEN", "")

    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    aws_profile: str = os.environ.get("AWS_PROFILE", "")
    aws_access_key_id: str = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_account_id: str = os.environ.get("AWS_ACCOUNT_ID", "")
    sqs_queue_name: str = os.environ.get("SQS_QUEUE_NAME", "TriggerCallQueue")
    # CALL_HISTORY event publishing (CallHistoryQueue).  Optional, same
    # opt-in pattern as GRAFANA_*: empty = events disabled, everything else
    # runs exactly as before.
    call_history_queue_name: str = os.environ.get("CALL_HISTORY_QUEUE_NAME", "")

    def require(self, name: str, val: str) -> None:
        if not val:
            raise SystemExit(f"Missing required env var: {name}")

    def resolve_profile(self, to_number: str) -> CountryProfile:
        # +56 routes to Switch (CL); everything else (incl. +55) routes to Twilio (BR) as fallback.
        cl = _PROFILE_MAP["+56"]
        if to_number.startswith(cl.prefix):
            cl.validate()
            return cl
        fallback = _PROFILE_MAP["+55"]
        fallback.validate()
        return fallback

    @property
    def profiles(self) -> dict[str, CountryProfile]:
        return _PROFILE_MAP

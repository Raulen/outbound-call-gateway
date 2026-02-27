from __future__ import annotations

import logging
from .config import BridgeConfig, CountryProfile


class ConfigDumper:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log

    def dump_effective_config(self) -> None:
        def _mask(value: str) -> str:
            if not value:
                return "(not set)"
            return f"{value[:4]}****"

        c = self._cfg
        self._log.info("=== Effective config ===")

        for prefix, profile in c.profiles.items():
            self._log.info(
                "[%s prefix=%s] LIVEKIT_URL=%s WSS=%s API_KEY=%s SIP_TRUNK=%s FROM=%s",
                profile.country_code,
                prefix,
                profile.livekit_url,
                profile.livekit_wss_url,
                _mask(profile.livekit_api_key),
                profile.sip_trunk_id,
                profile.sip_from_number,
            )

        self._log.info("ULTRAVOX_CALLS_URL=%s", c.ultravox_calls_url)
        self._log.info("ULTRAVOX_API_KEY=%s", _mask(c.ultravox_api_key))
        self._log.info("ULTRAVOX_VOICE=%s", c.ultravox_voice)
        self._log.info("SAMPLE_RATE=%d CHANNELS=%d FRAME_MS=%d", c.sample_rate, c.channels, c.frame_ms)
        self._log.info(
            "AWS_REGION=%s AWS_PROFILE=%s AWS_ACCOUNT_ID=%s SQS_QUEUE_NAME=%s",
            c.aws_region,
            c.aws_profile,
            c.aws_account_id,
            c.sqs_queue_name,
        )
        self._log.info("========================")

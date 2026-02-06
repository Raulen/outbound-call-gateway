from __future__ import annotations

import logging
from .config import BridgeConfig


class ConfigDumper:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log

    def dump_effective_config(self) -> None:
        c = self._cfg
        self._log.info("=== Effective config ===")
        self._log.info("LIVEKIT_URL=%s", c.livekit_url)
        self._log.info("LIVEKIT_WSS_URL=%s", c.livekit_wss_url)
        self._log.info("LIVEKIT_API_KEY=%s", c.livekit_api_key)
        self._log.info("LIVEKIT_API_SECRET=%s", c.livekit_api_secret)
        self._log.info("SIP_TRUNK_ID=%s", c.sip_trunk_id)
        self._log.info("SIP_FROM_NUMBER=%s", c.sip_from_number)
        self._log.info("ULTRAVOX_CALLS_URL=%s", c.ultravox_calls_url)
        self._log.info("ULTRAVOX_API_KEY=%s", c.ultravox_api_key)
        self._log.info("ULTRAVOX_VOICE=%s", c.ultravox_voice)
        self._log.info("SAMPLE_RATE=%d CHANNELS=%d FRAME_MS=%d", c.sample_rate, c.channels, c.frame_ms)
        self._log.info("AWS_REGION=%s AWS_PROFILE=%s AWS_ACCOUNT_ID=%s SQS_QUEUE_NAME=%s",
                       c.aws_region, c.aws_profile, c.aws_account_id, c.sqs_queue_name)
        self._log.info("========================")

from __future__ import annotations

import time
import logging
from typing import Optional, Dict, Any

import httpx

from .config import BridgeConfig


class UltravoxCallClient:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log

    async def create_ws_call_join_url(
            self,
            system_prompt: Optional[str] = None,
            *,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        self._cfg.require("ULTRAVOX_API_KEY", self._cfg.ultravox_api_key)
        self._cfg.require("ULTRAVOX_VOICE", self._cfg.ultravox_voice)

        prompt = system_prompt if system_prompt is not None else self._cfg.ultravox_system_prompt

        body = {
            "systemPrompt": prompt,
            "voice": self._cfg.ultravox_voice,
            "medium": {
                "serverWebSocket": {
                    "inputSampleRate": self._cfg.sample_rate,
                    "outputSampleRate": self._cfg.sample_rate,
                    "clientBufferSizeMs": 60,
                }
            },
        }

        if metadata is not None:
            body["metadata"] = metadata

        headers = {"X-API-Key": self._cfg.ultravox_api_key, "Content-Type": "application/json"}

        self._log.info("[Ultravox][REST] POST %s voice=%s inputSR=%d outputSR=%d",
                       self._cfg.ultravox_calls_url, self._cfg.ultravox_voice, self._cfg.sample_rate, self._cfg.sample_rate)

        t0 = time.time()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self._cfg.ultravox_calls_url, headers=headers, json=body)

        self._log.info("[Ultravox][REST] status=%s elapsedMs=%d", resp.status_code, int((time.time() - t0) * 1000))
        if resp.status_code >= 300:
            self._log.error("[Ultravox][REST] errorBody=%s", resp.text)
            resp.raise_for_status()

        data = resp.json()
        join_url = data.get("joinUrl")
        ultravox_call_id = data.get("callId") or data.get("id")
        self._log.info("[Ultravox][REST] callId=%s joinUrl=%s", ultravox_call_id, join_url)

        if not join_url:
            raise RuntimeError(f"Ultravox call created but joinUrl is missing. Response: {data}")

        return join_url

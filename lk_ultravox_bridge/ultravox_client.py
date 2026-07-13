from __future__ import annotations

import time
import logging
from typing import Optional, Dict, Any

import httpx

from .config import BridgeConfig

# Appended to every system prompt when voicemail hang-up is enabled.  Twilio
# Elastic SIP Trunking cannot do answering machine detection, so the model is
# the only thing in the pipeline that hears the voicemail greeting — this
# instruction turns it into the detector.  Kept in English on purpose: it is
# an operator instruction, not agent persona, and works for both pt-BR and
# es-CL calls regardless of the language of the caller-supplied prompt.
_VOICEMAIL_GUARD_PROMPT = (
    "\n\nIMPORTANT: If you determine the call was answered by a voicemail or "
    "answering machine (a recorded greeting, instructions to leave a message, "
    "or a beep), do NOT talk to the recording and do NOT leave a message: "
    "immediately call the hangUp tool to end the call."
)


class UltravoxCallClient:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log

    async def create_ws_call_join_url(
            self,
            system_prompt: Optional[str] = None,
            *,
            voice: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            greeting_message: Optional[str] = None,
            temperature: Optional[float] = None,
    ) -> str:
        self._cfg.require("ULTRAVOX_API_KEY", self._cfg.ultravox_api_key)

        resolved_voice = voice or self._cfg.ultravox_voice
        if not resolved_voice:
            raise SystemExit("Missing required voice: set ULTRAVOX_VOICE_BR/ULTRAVOX_VOICE_CL or ULTRAVOX_VOICE")

        prompt = system_prompt if system_prompt is not None else self._cfg.ultravox_system_prompt
        if self._cfg.ultravox_voicemail_hangup:
            prompt += _VOICEMAIL_GUARD_PROMPT

        # Outbound call: the callee answers and speaks first.  The fallback
        # makes the agent greet anyway if the callee stays silent after
        # pickup (e.g. answered but said nothing).
        greeting_fallback: Dict[str, Any] = {"delay": self._cfg.ultravox_greeting_delay}
        if greeting_message:
            greeting_fallback["text"] = greeting_message
        else:
            greeting_fallback["prompt"] = "Greet the user and introduce yourself."

        body = {
            "systemPrompt": prompt,
            "voice": resolved_voice,
            "temperature": temperature if temperature is not None else self._cfg.ultravox_temperature,
            "firstSpeakerSettings": {"user": {"fallback": greeting_fallback}},
            "joinTimeout": self._cfg.ultravox_join_timeout,
            "recordingEnabled": True,
            "medium": {
                "serverWebSocket": {
                    "inputSampleRate": self._cfg.sample_rate,
                    "outputSampleRate": self._cfg.sample_rate,
                    "clientBufferSizeMs": 60,
                }
            },
        }

        if self._cfg.ultravox_model:
            body["model"] = self._cfg.ultravox_model

        if self._cfg.ultravox_voicemail_hangup:
            # Built-in tool the guard prompt relies on.  hangUp's `strict`
            # parameter defaults to true, meaning further "user" speech (the
            # voicemail recording keeps talking) cannot cancel the hang-up.
            body["selectedTools"] = [{"toolName": "hangUp"}]

        if metadata is not None:
            body["metadata"] = metadata

        headers = {"X-API-Key": self._cfg.ultravox_api_key, "Content-Type": "application/json"}

        self._log.info("[Ultravox][REST] POST %s voice=%s inputSR=%d outputSR=%d",
                       self._cfg.ultravox_calls_url, resolved_voice, self._cfg.sample_rate, self._cfg.sample_rate)

        t0 = time.time()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self._cfg.ultravox_calls_url, headers=headers, json=body)

        elapsed_ms = int((time.time() - t0) * 1000)
        self._log.info("[Ultravox][REST] status=%s elapsedMs=%d", resp.status_code, elapsed_ms)
        if resp.status_code >= 300:
            self._log.error("[Ultravox][REST] errorBody=%s", resp.text)
            resp.raise_for_status()

        data = resp.json()
        join_url = data.get("joinUrl")
        ultravox_call_id = data.get("callId") or data.get("id")
        self._log.info("[Ultravox][REST] callId=%s joinUrl=%s elapsedMs=%d", ultravox_call_id, join_url, elapsed_ms)

        if not join_url:
            raise RuntimeError(f"Ultravox call created but joinUrl is missing. Response: {data}")

        return join_url

from __future__ import annotations

import time
import logging
from typing import NamedTuple, Optional, Dict, Any

import httpx

from .config import BridgeConfig

# Appended to every system prompt when voicemail hang-up is enabled.  Twilio
# Elastic SIP Trunking cannot do answering machine detection, so the model is
# the only thing in the pipeline that hears the voicemail greeting — this
# instruction turns it into the detector.  The guard is written in the call's
# language (selected by country profile): a large English block at the end of
# a pt-BR/es-CL prompt can pull the model's generation style (and perceived
# accent) toward English.  English is only the fallback for unknown countries.
_VOICEMAIL_GUARD_PROMPT = (
    "\n\nIMPORTANT: If the call is answered by a voicemail/answering machine, "
    "do NOT talk to the recording and do NOT leave a message: call the hangUp "
    "tool to end the call. Only do this on unambiguous evidence: a clearly "
    "pre-recorded greeting, explicit instructions to leave a message, or a "
    "recording beep. Silence, background noise, background voices or music, a "
    "distracted or hesitant answer, or someone speaking to another person "
    "nearby are NOT voicemail — real people often answer this way. When in "
    "doubt, assume it is a person and simply continue the conversation from "
    "where it is; never repeat a greeting you have already given, and never "
    "hang up on mere suspicion."
)

_VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY = {
    "BR": (
        "\n\nIMPORTANTE: Se a ligação for atendida por uma caixa postal ou "
        "secretária eletrônica, NÃO fale com a gravação e NÃO deixe recado: "
        "chame a ferramenta hangUp para encerrar a ligação. Só faça isso com "
        "evidência inequívoca: uma saudação claramente gravada, instruções "
        "explícitas para deixar recado, ou o bipe de gravação. Silêncio, ruído "
        "de fundo, vozes ou música ao fundo, uma resposta distraída ou "
        "hesitante, ou alguém falando com outra pessoa por perto NÃO são caixa "
        "postal — pessoas reais atendem assim com frequência. Na dúvida, "
        "assuma que é uma pessoa e simplesmente continue a conversa de onde "
        "está; nunca repita uma saudação que já fez e nunca desligue por mera "
        "suspeita."
    ),
    "CL": (
        "\n\nIMPORTANTE: Si la llamada es atendida por un buzón de voz o "
        "contestador automático, NO hables con la grabación y NO dejes "
        "mensaje: llama a la herramienta hangUp para terminar la llamada. "
        "Hazlo solo con evidencia inequívoca: un saludo claramente pregrabado, "
        "instrucciones explícitas para dejar un mensaje, o el pitido de "
        "grabación. El silencio, el ruido de fondo, voces o música de fondo, "
        "una respuesta distraída o vacilante, o alguien hablando con otra "
        "persona cerca NO son buzón de voz — las personas reales suelen "
        "contestar así. Ante la duda, asume que es una persona y simplemente "
        "continúa la conversación donde está; nunca repitas un saludo que ya "
        "diste y nunca cuelgues por mera sospecha."
    ),
}


class UltravoxCall(NamedTuple):
    """Result of creating an Ultravox call: the WS to join and the call's id
    (used for post-call correlation with recordings/transcripts)."""

    join_url: str
    call_id: Optional[str]


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
            country_code: Optional[str] = None,
            language_hint: Optional[str] = None,
    ) -> UltravoxCall:
        self._cfg.require("ULTRAVOX_API_KEY", self._cfg.ultravox_api_key)

        resolved_voice = voice or self._cfg.ultravox_voice
        if not resolved_voice:
            raise SystemExit("Missing required voice: set ULTRAVOX_VOICE_BR/ULTRAVOX_VOICE_CL or ULTRAVOX_VOICE")

        prompt = system_prompt if system_prompt is not None else self._cfg.ultravox_system_prompt
        if self._cfg.ultravox_voicemail_hangup:
            prompt += _VOICEMAIL_GUARD_PROMPTS_BY_COUNTRY.get(country_code or "", _VOICEMAIL_GUARD_PROMPT)

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

        # BCP47 hint guiding Ultravox ASR and TTS toward the call's language;
        # without it the platform auto-detects, which we saw drift to the
        # wrong language on noisy SIP audio.
        if language_hint:
            body["languageHint"] = language_hint

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

        return UltravoxCall(join_url=join_url, call_id=ultravox_call_id)

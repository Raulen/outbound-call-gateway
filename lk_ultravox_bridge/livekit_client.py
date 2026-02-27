from __future__ import annotations

import time
import logging
from dataclasses import dataclass

from livekit import rtc
import livekit.api as api

from .config import BridgeConfig, CountryProfile


class LiveKitTokenFactory:
    def __init__(self, profile: CountryProfile):
        self._profile = profile

    def generate_token(self, room: str, identity: str) -> str:
        at = api.AccessToken(self._profile.livekit_api_key, self._profile.livekit_api_secret)
        grants = api.VideoGrants(room_join=True, can_publish=True, can_subscribe=True, room=room)
        return (
            at.with_identity(identity)
              .with_name("LiveKitUltravoxBridge")
              .with_grants(grants)
              .to_jwt()
        )


class LiveKitSipDialer:
    def __init__(self, log: logging.Logger):
        self._log = log

    async def dial_out(self, room_name: str, to_number: str, profile: CountryProfile) -> None:
        profile.validate()

        lk = api.LiveKitAPI(profile.livekit_url, profile.livekit_api_key, profile.livekit_api_secret)

        req = api.CreateSIPParticipantRequest(
            sip_trunk_id=profile.sip_trunk_id,
            sip_call_to=to_number,
            sip_number=profile.sip_from_number,
            room_name=room_name,
            participant_identity=f"sip-{to_number}",
            participant_name=to_number,
            wait_until_answered=True,
            krisp_enabled=True,
        )

        t0 = time.time()
        self._log.info(
            "[LiveKit][SIP] CreateSIPParticipant to=%s trunk=%s from=%s room=%s country=%s",
            to_number, profile.sip_trunk_id, profile.sip_from_number, room_name, profile.country_code,
        )

        try:
            resp = await lk.sip.create_sip_participant(req)
        except Exception:
            self._log.error(
                "[LiveKit][SIP] failed to create SIP participant to=%s room=%s",
                to_number,
                room_name,
                exc_info=True,
            )
            raise

        elapsed_ms = int((time.time() - t0) * 1000)
        self._log.info(
            "[LiveKit][SIP] ok elapsedMs=%d participantId=%s identity=%s sipCallId=%s room=%s",
            elapsed_ms,
            getattr(resp, "participant_id", None),
            getattr(resp, "participant_identity", None),
            getattr(resp, "sip_call_id", None),
            getattr(resp, "room_name", None),
        )


@dataclass
class LiveKitSession:
    room: rtc.Room
    audio_source: rtc.AudioSource
    local_track: rtc.LocalAudioTrack


class LiveKitRoomConnector:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger, token_factory: LiveKitTokenFactory, profile: CountryProfile):
        self._cfg = cfg
        self._log = log
        self._token_factory = token_factory
        self._profile = profile

    async def connect_and_publish(self, room_name: str, identity: str, on_events) -> LiveKitSession:
        room = rtc.Room()
        on_events(room)

        token = self._token_factory.generate_token(room_name, identity)
        self._log.info(
            "[LiveKit][RTC] connecting room=%s identity=%s wss=%s",
            room_name, identity, self._profile.livekit_wss_url,
        )
        await room.connect(self._profile.livekit_wss_url, token)
        self._log.info("[LiveKit][RTC] Connected room=%s identity=%s", room_name, identity)

        audio_source = rtc.AudioSource(self._cfg.sample_rate, self._cfg.channels)
        local_track = rtc.LocalAudioTrack.create_audio_track("ultravox-agent-audio", audio_source)

        t0 = time.time()
        await room.local_participant.publish_track(local_track)
        self._log.info(
            "[LiveKit][RTC] Published local track elapsedMs=%d sampleRate=%d channels=%d",
            int((time.time() - t0) * 1000), self._cfg.sample_rate, self._cfg.channels,
        )

        return LiveKitSession(room=room, audio_source=audio_source, local_track=local_track)

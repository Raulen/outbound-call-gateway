from __future__ import annotations

import time
import logging
from dataclasses import dataclass
from typing import Optional

from livekit import rtc
import livekit.api as api

from .config import BridgeConfig, CountryProfile


class CallNotAnsweredError(Exception):
    """The callee could not be reached: rang out, busy, declined, phone off
    or invalid number.  This is a business outcome, not a system failure —
    the SQS worker acks the message instead of retrying (redial policy
    belongs to the campaign system, never to a 5-minute visibility loop).
    """

    def __init__(self, reason: str, sip_status: Optional[int] = None):
        super().__init__(f"call not completed: {reason} (sipStatus={sip_status})")
        self.reason = reason
        self.sip_status = sip_status


# SIP status -> unreachable-callee category.  Only 408 has been observed in
# real traffic; the remaining entries are standard SIP semantics to be
# validated against production (every dial failure logs its raw fields so
# a wrong mapping shows up with evidence).
_UNREACHABLE_SIP_STATUS = {
    408: "no-answer",       # rang until the carrier gave up
    486: "busy",            # Busy Here
    600: "busy",            # Busy Everywhere
    603: "declined",        # callee rejected the call
    480: "unavailable",     # phone off / out of coverage
    404: "invalid-number",
    484: "invalid-number",  # Address Incomplete
}


def _classify_dial_failure(exc: Exception) -> Optional[CallNotAnsweredError]:
    """Maps a create_sip_participant failure to an unreachable-callee
    category, or None when it looks like a genuine system error (trunk
    auth, network, 5xx) that should keep the current retry semantics.

    Inspects attributes defensively (never the exception class): the SDK's
    error surface has changed across versions.
    """
    status = getattr(exc, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    reason = _UNREACHABLE_SIP_STATUS.get(status)
    if reason is None:
        message = str(getattr(exc, "message", "") or exc).lower()
        if "request timed out" in message:  # some SDK versions omit status
            reason = "no-answer"

    return CallNotAnsweredError(reason, status) if reason else None


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
            async with api.LiveKitAPI(profile.livekit_url, profile.livekit_api_key, profile.livekit_api_secret) as lk:
                resp = await lk.sip.create_sip_participant(req)
        except Exception as e:
            not_answered = _classify_dial_failure(e)
            if not_answered is not None:
                # Expected telephony outcome: one clean line, no traceback.
                # Raw fields included so a wrong status mapping is visible.
                self._log.warning(
                    "[LiveKit][SIP] not answered to=%s room=%s reason=%s sipStatus=%s rawCode=%s rawMessage=%s",
                    to_number, room_name, not_answered.reason, not_answered.sip_status,
                    getattr(e, "code", None), str(getattr(e, "message", "") or e)[:120],
                )
                raise not_answered from e
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


class LiveKitRoomTerminator:
    """Deletes the LiveKit room once a call is over.

    room.disconnect() only detaches *our* RTC client; the SIP participant —
    and the carrier's billable phone leg behind it — stays up until the callee
    hangs up.  When it is our side that ends the call (voicemail hangUp,
    silence watchdog, bridge error), deleting the room is what forcefully
    removes the SIP participant and sends BYE to the trunk.
    """

    def __init__(self, log: logging.Logger):
        self._log = log

    async def terminate(self, room_name: str, profile: CountryProfile) -> None:
        try:
            async with api.LiveKitAPI(profile.livekit_url, profile.livekit_api_key, profile.livekit_api_secret) as lk:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            self._log.info("[LiveKit][API] room deleted room=%s", room_name)
        except Exception:
            # Best-effort: the room may already be gone (callee hung up first
            # and LiveKit closed the empty room).  Teardown failure must never
            # mask the bridge's own outcome.
            self._log.warning("[LiveKit][API] failed to delete room=%s", room_name, exc_info=True)


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

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from livekit import rtc

from .config import BridgeConfig, CountryProfile
from .livekit_client import LiveKitTokenFactory, LiveKitRoomConnector, LiveKitRoomTerminator, LiveKitSession
from .audio_bridge import AudioBridge


class BridgeAgent:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger, room_name: str, profile: CountryProfile):
        self._cfg = cfg
        self._profile = profile
        self._log = log
        self.room_name = room_name
        self.identity = f"lk-uv-bridge-{uuid.uuid4().hex[:6]}"

        self.session: Optional[LiveKitSession] = None
        self.remote_audio_track: Optional[rtc.RemoteAudioTrack] = None
        self._remote_track_ready = asyncio.Event()
        self._stop = asyncio.Event()

    async def connect_livekit(self) -> None:
        self._log.info(
            "[Bridge] connecting to LiveKit room=%s identity=%s country=%s",
            self.room_name, self.identity, self._profile.country_code,
        )
        token_factory = LiveKitTokenFactory(self._profile)
        connector = LiveKitRoomConnector(self._cfg, self._log, token_factory, self._profile)

        def _register_handlers(room: rtc.Room):
            @room.on("track_subscribed")
            def _on_track(track, publication, participant):
                if isinstance(track, rtc.RemoteAudioTrack) and not self.remote_audio_track:
                    self.remote_audio_track = track
                    self._log.info(
                        "[LiveKit][RTC] Remote audio track subscribed participant=%s sid=%s trackSid=%s",
                        participant.identity, participant.sid, getattr(publication, "sid", None),
                    )
                    self._remote_track_ready.set()

            @room.on("participant_connected")
            def _on_participant(p):
                self._log.info("[LiveKit][RTC] Participant connected identity=%s sid=%s", p.identity, p.sid)

            @room.on("participant_disconnected")
            def _on_participant_disc(p):
                self._log.info("[LiveKit][RTC] Participant disconnected identity=%s sid=%s", p.identity, p.sid)
                if p.identity and p.identity.startswith("sip-"):
                    self._log.info(
                        "[Bridge] stop requested because SIP participant left room=%s identity=%s",
                        self.room_name, p.identity,
                    )
                    self._stop.set()

            @room.on("disconnected")
            def _on_disc():
                self._log.info("[LiveKit][RTC] Room disconnected; requesting bridge stop room=%s", self.room_name)
                self._stop.set()

        self.session = await connector.connect_and_publish(self.room_name, self.identity, _register_handlers)
        self._log.info("[Bridge] LiveKit session established room=%s identity=%s", self.room_name, self.identity)

    async def run_bridge(self, ultravox_join_url: str, *, ws=None,
                         remote_track_timeout: Optional[float] = None) -> None:
        """Bridge audio until the call ends, then tear the room down.

        remote_track_timeout bounds the wait for the SIP audio track.  Pass a
        value when the dial is known to be answered (SQS worker); leave None
        when waiting indefinitely is intended (CLI inbound mode).
        """
        if not self.session:
            raise RuntimeError("Connect LiveKit first")

        try:
            self._log.info("[Bridge] run_bridge start joinUrl=%s", ultravox_join_url)
            self._log.info("[Bridge] Waiting for remote SIP audio track...")
            if remote_track_timeout is not None:
                await asyncio.wait_for(self._remote_track_ready.wait(), remote_track_timeout)
            else:
                await self._remote_track_ready.wait()

            if not self.remote_audio_track:
                raise RuntimeError("Remote track ready event set, but track is None")

            self._log.info("[Bridge] starting audio bridge room=%s", self.room_name)
            await AudioBridge(self._cfg, self._log).run(
                join_url=ultravox_join_url,
                remote_audio_track=self.remote_audio_track,
                audio_source=self.session.audio_source,
                stop_evt=self._stop,
                ws=ws,
            )
            self._log.info("[Bridge] audio bridge completed room=%s stopFlag=%s", self.room_name, self._stop.is_set())
        finally:
            await self.teardown()

    async def teardown(self) -> None:
        """Disconnect the RTC client and delete the room (best-effort).

        Disconnecting only detaches our client; deleting the room is what
        removes the SIP participant and sends BYE to the trunk when we are
        the side ending (or abandoning) the call.
        """
        if self.session:
            try:
                await self.session.room.disconnect()
                self._log.info("[Bridge] LiveKit room disconnected room=%s", self.room_name)
            except Exception:
                self._log.warning("[Bridge] error disconnecting LiveKit room=%s", self.room_name, exc_info=True)
        await LiveKitRoomTerminator(self._log).terminate(self.room_name, self._profile)

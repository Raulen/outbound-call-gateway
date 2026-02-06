from __future__ import annotations

import asyncio
import json
import time
import logging

import websockets
from livekit import rtc

from .config import BridgeConfig


class AudioBridge:
    def __init__(self, cfg: BridgeConfig, log: logging.Logger):
        self._cfg = cfg
        self._log = log

    async def run(self, *, join_url: str, remote_audio_track: rtc.RemoteAudioTrack, audio_source: rtc.AudioSource, stop_evt: asyncio.Event) -> None:
        self._log.info("[Bridge] Remote track ready -> connecting to Ultravox WS")

        t0 = time.time()
        async with websockets.connect(
            join_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as ws:
            self._log.info("[Ultravox][WS] connected elapsedMs=%d", int((time.time() - t0) * 1000))

            t_in = asyncio.create_task(self._livekit_to_ultravox(ws, remote_audio_track, stop_evt))
            t_out = asyncio.create_task(self._ultravox_to_livekit(ws, audio_source, stop_evt))
            t_stop = asyncio.create_task(stop_evt.wait())

            done, pending = await asyncio.wait({t_in, t_out, t_stop}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            for task in done:
                if task in (t_in, t_out):
                    exc = task.exception()
                    if exc:
                        raise exc

            self._log.info("[Bridge] finished (stop=%s)", stop_evt.is_set())

    async def _livekit_to_ultravox(self, ws, remote_audio_track: rtc.RemoteAudioTrack, stop_evt: asyncio.Event) -> None:
        audio_stream = rtc.AudioStream.from_track(
            track=remote_audio_track,
            sample_rate=self._cfg.sample_rate,
            num_channels=self._cfg.channels,
            frame_size_ms=self._cfg.frame_ms,
        )

        frames = 0
        bytes_sent = 0
        first = True
        last_log = time.time()

        self._log.info("[LK->UV] stream start sampleRate=%d channels=%d frameMs=%d", self._cfg.sample_rate, self._cfg.channels, self._cfg.frame_ms)

        try:
            async for event in audio_stream:
                payload = bytes(event.frame.data)
                if first:
                    first = False
                    self._log.info("[LK->UV] first frame bytes=%d", len(payload))

                await ws.send(payload)
                frames += 1
                bytes_sent += len(payload)

                now = time.time()
                if now - last_log >= 2.0:
                    kbps = (bytes_sent * 8) / (now - last_log) / 1000.0
                    self._log.info("[LK->UV] ok frames=%d bytes=%d approxKbps=%.1f", frames, bytes_sent, kbps)
                    frames = 0
                    bytes_sent = 0
                    last_log = now
        finally:
            await audio_stream.aclose()
            self._log.info("[LK->UV] stream stopped")
            stop_evt.set()

    async def _ultravox_to_livekit(self, ws, audio_source: rtc.AudioSource, stop_evt: asyncio.Event) -> None:
        samples_per_frame = int(self._cfg.sample_rate * (self._cfg.frame_ms / 1000.0))
        bytes_per_frame = samples_per_frame * 2 * self._cfg.channels

        buf = bytearray()
        frames = 0
        bytes_recv = 0
        first_audio = True
        last_log = time.time()

        self._log.info("[UV->LK] stream start samplesPerFrame=%d bytesPerFrame=%d", samples_per_frame, bytes_per_frame)

        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    bytes_recv += len(msg)
                    buf.extend(msg)

                    if first_audio:
                        first_audio = False
                        self._log.info("[UV->LK] first audio chunk bytes=%d (bufferBytes=%d)", len(msg), len(buf))

                    while len(buf) >= bytes_per_frame:
                        chunk = bytes(buf[:bytes_per_frame])
                        del buf[:bytes_per_frame]

                        frame = rtc.AudioFrame.create(self._cfg.sample_rate, self._cfg.channels, samples_per_frame)
                        dst_bytes = frame.data.cast("B")
                        dst_bytes[:len(chunk)] = chunk
                        await audio_source.capture_frame(frame)
                        frames += 1

                    now = time.time()
                    if now - last_log >= 2.0:
                        kbps = (bytes_recv * 8) / (now - last_log) / 1000.0
                        self._log.info("[UV->LK] ok frames=%d recvBytes=%d bufferBytes=%d approxKbps=%.1f",
                                       frames, bytes_recv, len(buf), kbps)
                        frames = 0
                        bytes_recv = 0
                        last_log = now
                else:
                    try:
                        data = json.loads(msg)
                    except Exception:
                        self._log.warning("[UV->LK] non-JSON text message: %r", msg)
                        continue

                    if data.get("type") == "playbackClearBuffer":
                        audio_source.clear_queue()
                        buf.clear()
                        self._log.info("[UV->LK] playbackClearBuffer -> cleared LK queue + local buffer")
                    else:
                        self._log.info("[Ultravox][WS][data] %s", data)
        finally:
            self._log.info("[UV->LK] stream stopped")
            stop_evt.set()

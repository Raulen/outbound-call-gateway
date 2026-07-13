"""The audio bridge is the product: PCM in both directions, frame assembly,
jitter-buffer overflow recovery, barge-in, and teardown guarantees.

Test geometry (from make_config): sample_rate=16000, frame_ms=20 ->
320 samples/frame -> 640 bytes/frame (16-bit mono).
max_buffer_frames=5 (3200 bytes), keep_buffer_frames=2 (1280 bytes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from livekit import rtc

from lk_ultravox_bridge.audio_bridge import AudioBridge

from tests.conftest import FakeAudioSource, FakeAudioStream, FakeWS, make_config

log = logging.getLogger("test")

BYTES_PER_FRAME = 640


def frame_bytes(tag: int, length: int = BYTES_PER_FRAME) -> bytes:
    """Identifiable PCM payload: every byte carries the frame's tag."""
    return bytes([tag]) * length


def make_bridge(**cfg_overrides) -> AudioBridge:
    return AudioBridge(make_config(**cfg_overrides), log)


async def run_uv_to_lk(ws: FakeWS, source: FakeAudioSource, **cfg_overrides) -> asyncio.Event:
    stop_evt = asyncio.Event()
    await make_bridge(**cfg_overrides)._ultravox_to_livekit(ws, source, stop_evt)
    return stop_evt


class TestFrameAssembly:
    async def test_chunks_smaller_than_frame_accumulate_into_one_frame(self):
        source = FakeAudioSource()
        # 200 + 200 + 240 = exactly one 640-byte frame
        ws = FakeWS([b"\x01" * 200, b"\x02" * 200, b"\x03" * 240])
        await run_uv_to_lk(ws, source)
        assert source.captured == [b"\x01" * 200 + b"\x02" * 200 + b"\x03" * 240]

    async def test_large_chunk_yields_multiple_frames_and_keeps_remainder(self):
        source = FakeAudioSource()
        # 2.5 frames in one WS message: 2 complete frames out, 320 bytes held back
        payload = frame_bytes(1) + frame_bytes(2) + b"\x03" * 320
        ws = FakeWS([payload])
        await run_uv_to_lk(ws, source)
        assert source.captured == [frame_bytes(1), frame_bytes(2)]

    async def test_remainder_completes_with_next_message(self):
        source = FakeAudioSource()
        ws = FakeWS([b"\x01" * 320, b"\x01" * 320 + frame_bytes(2)])
        await run_uv_to_lk(ws, source)
        assert source.captured == [b"\x01" * 640, frame_bytes(2)]

    async def test_exact_multiple_of_frame_size_leaves_no_remainder(self):
        source = FakeAudioSource()
        ws = FakeWS([frame_bytes(1) + frame_bytes(2)])
        await run_uv_to_lk(ws, source)
        assert source.captured == [frame_bytes(1), frame_bytes(2)]

    async def test_captured_frames_have_correct_rtc_geometry(self):
        # AudioFrame must be created with the configured rate/channels,
        # otherwise LiveKit resamples garbage.
        captured_frames = []

        class GeometrySource(FakeAudioSource):
            async def capture_frame(self, frame):
                captured_frames.append(frame)
                await super().capture_frame(frame)

        await run_uv_to_lk(FakeWS([frame_bytes(1)]), GeometrySource())
        frame = captured_frames[0]
        assert frame.sample_rate == 16000
        assert frame.num_channels == 1
        assert frame.samples_per_channel == 320


class TestJitterBufferOverflow:
    async def test_burst_beyond_max_drops_oldest_and_keeps_newest(self, caplog):
        source = FakeAudioSource()
        # 8 frames at once: available (5120) > max (3200).
        # excess = 5120 - keep (1280) = 3840 -> drop 6 oldest frames,
        # play only the newest 2.
        burst = b"".join(frame_bytes(i) for i in range(1, 9))
        with caplog.at_level(logging.WARNING):
            await run_uv_to_lk(FakeWS([burst]), source)

        assert source.captured == [frame_bytes(7), frame_bytes(8)]
        assert "buffer overflow" in caplog.text
        assert "dropped 6 frames" in caplog.text

    async def test_drop_is_frame_aligned_with_partial_tail(self):
        source = FakeAudioSource()
        # 8 frames + 100 stray bytes: the cut must land on a frame boundary,
        # so the 100-byte tail stays buffered (never played as a broken frame).
        burst = b"".join(frame_bytes(i) for i in range(1, 9)) + b"\xff" * 100
        await run_uv_to_lk(FakeWS([burst]), source)
        assert source.captured == [frame_bytes(7), frame_bytes(8)]

    async def test_buffer_at_exactly_max_is_not_dropped(self):
        source = FakeAudioSource()
        # 5 frames == max_buffer_bytes: threshold is strict (>), so all play.
        burst = b"".join(frame_bytes(i) for i in range(1, 6))
        await run_uv_to_lk(FakeWS([burst]), source)
        assert source.captured == [frame_bytes(i) for i in range(1, 6)]


class TestBargeIn:
    @pytest.mark.parametrize("event_name", ["playbackClearBuffer", "playback_clear_buffer"])
    async def test_clear_buffer_event_clears_queue_and_local_buffer(self, event_name):
        source = FakeAudioSource()
        ws = FakeWS([
            b"\x01" * 200,                        # partial frame, still buffered
            json.dumps({"type": event_name}),     # user barged in
            frame_bytes(2),                       # agent's new utterance
        ])
        await run_uv_to_lk(ws, source)

        assert source.clear_queue_calls == 1
        # The stale 200 bytes must NOT leak into the post-barge-in frame.
        assert source.captured == [frame_bytes(2)]

    async def test_unknown_json_event_does_not_clear(self):
        source = FakeAudioSource()
        ws = FakeWS([json.dumps({"type": "transcript", "text": "hi"}), frame_bytes(1)])
        await run_uv_to_lk(ws, source)
        assert source.clear_queue_calls == 0
        assert source.captured == [frame_bytes(1)]

    async def test_non_json_text_is_tolerated(self):
        source = FakeAudioSource()
        ws = FakeWS(["not-json-at-all", frame_bytes(1)])
        await run_uv_to_lk(ws, source)  # must not raise
        assert source.captured == [frame_bytes(1)]


class TestTeardown:
    async def test_uv_to_lk_sets_stop_event_when_ws_ends(self):
        stop_evt = await run_uv_to_lk(FakeWS([frame_bytes(1)]), FakeAudioSource())
        assert stop_evt.is_set()

    async def test_uv_to_lk_sets_stop_event_even_on_error(self):
        stop_evt = asyncio.Event()
        ws = FakeWS([frame_bytes(1)], iter_error=RuntimeError("ws died"))
        with pytest.raises(RuntimeError, match="ws died"):
            await make_bridge()._ultravox_to_livekit(ws, FakeAudioSource(), stop_evt)
        assert stop_evt.is_set()


class TestSilenceWatchdog:
    async def test_prolonged_silence_aborts_the_call(self):
        stop_evt = asyncio.Event()
        stale_since = time.time() - 100  # last message "100s ago"
        await asyncio.wait_for(
            make_bridge()._uv_silence_watchdog(
                stop_evt, lambda: stale_since, threshold_s=0.5, check_interval_s=0.01
            ),
            timeout=2.0,
        )
        assert stop_evt.is_set()

    async def test_fresh_messages_keep_the_call_alive(self):
        stop_evt = asyncio.Event()
        task = asyncio.create_task(
            make_bridge()._uv_silence_watchdog(
                stop_evt, time.time, threshold_s=5.0, check_interval_s=0.01
            )
        )
        await asyncio.sleep(0.1)  # many check intervals pass
        assert not stop_evt.is_set()
        task.cancel()

    async def test_watchdog_exits_when_stop_already_set(self):
        stop_evt = asyncio.Event()
        stop_evt.set()
        await asyncio.wait_for(
            make_bridge()._uv_silence_watchdog(
                stop_evt, lambda: 0.0, threshold_s=0.5, check_interval_s=0.01
            ),
            timeout=2.0,
        )


@pytest.fixture
def patched_audio_stream(monkeypatch):
    """Route rtc.AudioStream.from_track to a controllable fake."""
    holder = {}

    def fake_from_track(**kwargs):
        holder["from_track_kwargs"] = kwargs
        return holder["stream"]

    monkeypatch.setattr(rtc.AudioStream, "from_track", staticmethod(fake_from_track))

    def install(stream: FakeAudioStream) -> dict:
        holder["stream"] = stream
        return holder

    return install


class TestLiveKitToUltravox:
    async def test_sip_frames_are_forwarded_raw_to_ws(self, patched_audio_stream):
        stream = FakeAudioStream([bytearray(frame_bytes(1)), bytearray(frame_bytes(2))])
        holder = patched_audio_stream(stream)
        ws = FakeWS()
        stop_evt = asyncio.Event()

        await make_bridge()._livekit_to_ultravox(ws, remote_audio_track="fake-track", stop_evt=stop_evt)

        assert ws.sent == [frame_bytes(1), frame_bytes(2)]
        assert stream.closed
        assert stop_evt.is_set()
        # The stream must be built with the configured audio geometry.
        kwargs = holder["from_track_kwargs"]
        assert kwargs["sample_rate"] == 16000
        assert kwargs["num_channels"] == 1
        assert kwargs["frame_size_ms"] == 20

    async def test_ws_send_failure_propagates_and_stops(self, patched_audio_stream):
        patched_audio_stream(FakeAudioStream([bytearray(frame_bytes(1))]))
        ws = FakeWS(send_error=ConnectionError("ws closed"))
        stop_evt = asyncio.Event()

        with pytest.raises(ConnectionError):
            await make_bridge()._livekit_to_ultravox(ws, remote_audio_track="fake-track", stop_evt=stop_evt)
        assert stop_evt.is_set()


class TestRunStreams:
    """AudioBridge.run(..., ws=...) exercises _run_streams end to end."""

    async def run_bridge(self, ws: FakeWS, stream: FakeAudioStream, patched, *, prestop=False):
        patched(stream)
        source = FakeAudioSource()
        stop_evt = asyncio.Event()
        if prestop:
            stop_evt.set()
        await make_bridge().run(
            join_url="wss://unused.test",
            remote_audio_track="fake-track",
            audio_source=source,
            stop_evt=stop_evt,
            ws=ws,
        )
        return source, stop_evt

    async def test_stop_event_shuts_down_both_directions(self, patched_audio_stream):
        # Both legs would run forever; the pre-set stop event must win.
        source, _ = await asyncio.wait_for(
            self.run_bridge(FakeWS(hang=True), FakeAudioStream(hang=True),
                            patched_audio_stream, prestop=True),
            timeout=2.0,
        )
        assert source.captured == []

    async def test_uv_leg_error_propagates_through_run(self, patched_audio_stream):
        patched_audio_stream(FakeAudioStream(hang=True))
        ws = FakeWS([frame_bytes(1)], iter_error=RuntimeError("uv leg died"))
        with pytest.raises(RuntimeError, match="uv leg died"):
            await asyncio.wait_for(
                make_bridge().run(
                    join_url="wss://unused.test",
                    remote_audio_track="fake-track",
                    audio_source=FakeAudioSource(),
                    stop_evt=asyncio.Event(),
                    ws=ws,
                ),
                timeout=2.0,
            )

    async def test_one_leg_finishing_stops_the_other(self, patched_audio_stream):
        # SIP leg ends cleanly (caller hung up at the RTC level) while the
        # Ultravox leg would stream forever: the bridge must still return.
        source, stop_evt = await asyncio.wait_for(
            self.run_bridge(FakeWS(hang=True), FakeAudioStream([bytearray(frame_bytes(1))]),
                            patched_audio_stream),
            timeout=2.0,
        )
        assert stop_evt.is_set()

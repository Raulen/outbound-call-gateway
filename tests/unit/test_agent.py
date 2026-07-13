"""BridgeAgent lifecycle: event handlers decide when a live phone call ends,
and run_bridge's finally block is what prevents leaked RTC connections."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from livekit import rtc

import lk_ultravox_bridge.agent as agent_module
from lk_ultravox_bridge.agent import BridgeAgent
from lk_ultravox_bridge.livekit_client import LiveKitSession

from tests.conftest import FakeAudioSource, make_config, make_profile

log = logging.getLogger("test")


class FakeRoom:
    def __init__(self):
        self.handlers = {}
        self.disconnect_calls = 0

    def on(self, event_name):
        def register(fn):
            self.handlers[event_name] = fn
            return fn
        return register

    async def disconnect(self):
        self.disconnect_calls += 1


def fake_remote_audio_track() -> rtc.RemoteAudioTrack:
    # isinstance-compatible instance without touching the native layer.
    return object.__new__(rtc.RemoteAudioTrack)


class FakeTerminator:
    """Records room deletions instead of calling the LiveKit API."""

    calls: list = []

    def __init__(self, log):
        pass

    async def terminate(self, room_name, profile):
        FakeTerminator.calls.append((room_name, profile))


def make_agent() -> BridgeAgent:
    return BridgeAgent(make_config(), log, "room-test", make_profile())


@pytest.fixture
async def connected(monkeypatch):
    """An agent whose connect_livekit ran against a FakeRoom."""
    room = FakeRoom()
    source = FakeAudioSource()

    class FakeConnector:
        def __init__(self, cfg, log, token_factory, profile):
            pass

        async def connect_and_publish(self, room_name, identity, on_events):
            on_events(room)
            return LiveKitSession(room=room, audio_source=source, local_track=None)

    monkeypatch.setattr(agent_module, "LiveKitRoomConnector", FakeConnector)
    agent = make_agent()
    await agent.connect_livekit()
    return agent, room


class TestEventHandlers:
    async def test_first_remote_audio_track_is_captured(self, connected):
        agent, room = connected
        track = fake_remote_audio_track()
        publication = SimpleNamespace(sid="TR_1")
        participant = SimpleNamespace(identity="sip-+5511", sid="PA_1")

        room.handlers["track_subscribed"](track, publication, participant)

        assert agent.remote_audio_track is track
        assert agent._remote_track_ready.is_set()

    async def test_second_audio_track_does_not_replace_first(self, connected):
        agent, room = connected
        first, second = fake_remote_audio_track(), fake_remote_audio_track()
        pub = SimpleNamespace(sid="TR")
        part = SimpleNamespace(identity="sip-+5511", sid="PA")

        room.handlers["track_subscribed"](first, pub, part)
        room.handlers["track_subscribed"](second, pub, part)

        assert agent.remote_audio_track is first

    async def test_non_audio_track_is_ignored(self, connected):
        agent, room = connected
        room.handlers["track_subscribed"](
            object(), SimpleNamespace(sid="TR"), SimpleNamespace(identity="x", sid="PA")
        )
        assert agent.remote_audio_track is None
        assert not agent._remote_track_ready.is_set()

    async def test_sip_participant_leaving_stops_the_bridge(self, connected):
        agent, room = connected
        room.handlers["participant_disconnected"](
            SimpleNamespace(identity="sip-+5511999998888", sid="PA_1")
        )
        assert agent._stop.is_set()

    async def test_non_sip_participant_leaving_does_not_stop(self, connected):
        # e.g. a monitoring/recording participant dropping must not kill the call.
        agent, room = connected
        room.handlers["participant_disconnected"](
            SimpleNamespace(identity="observer-1", sid="PA_2")
        )
        assert not agent._stop.is_set()

    async def test_room_disconnect_stops_the_bridge(self, connected):
        agent, room = connected
        room.handlers["disconnected"]()
        assert agent._stop.is_set()


class TestRunBridge:
    async def test_requires_connect_first(self):
        agent = make_agent()
        with pytest.raises(RuntimeError, match="Connect LiveKit first"):
            await agent.run_bridge("wss://uv.test/join")

    async def _prepared_agent(self, monkeypatch, bridge_run):
        """Agent with a fake session, a ready remote track, and a stubbed AudioBridge."""
        monkeypatch.setattr(agent_module, "LiveKitRoomTerminator", FakeTerminator)
        FakeTerminator.calls = []
        room = FakeRoom()
        agent = make_agent()
        agent.session = LiveKitSession(room=room, audio_source=FakeAudioSource(), local_track=None)
        agent.remote_audio_track = fake_remote_audio_track()
        agent._remote_track_ready.set()

        class FakeAudioBridge:
            def __init__(self, cfg, log):
                pass

            async def run(self, **kwargs):
                await bridge_run(**kwargs)

        monkeypatch.setattr(agent_module, "AudioBridge", FakeAudioBridge)
        return agent, room

    async def test_disconnects_room_after_clean_bridge(self, monkeypatch):
        async def clean_run(**kwargs):
            pass

        agent, room = await self._prepared_agent(monkeypatch, clean_run)
        await agent.run_bridge("wss://uv.test/join")
        assert room.disconnect_calls == 1
        # room.disconnect() alone leaves the SIP participant (and the carrier
        # phone leg) alive; the room must also be deleted server-side.
        assert FakeTerminator.calls == [("room-test", agent._profile)]

    async def test_disconnects_room_even_when_bridge_raises(self, monkeypatch):
        # A leaked RTC connection per failed call was a real production bug;
        # the finally-disconnect is the regression guard.
        async def failing_run(**kwargs):
            raise ConnectionError("ultravox ws died")

        agent, room = await self._prepared_agent(monkeypatch, failing_run)
        with pytest.raises(ConnectionError):
            await agent.run_bridge("wss://uv.test/join")
        assert room.disconnect_calls == 1
        assert FakeTerminator.calls == [("room-test", agent._profile)]

    async def test_waits_for_remote_track_before_bridging(self, monkeypatch):
        started = asyncio.Event()

        async def marking_run(**kwargs):
            started.set()

        agent, _ = await self._prepared_agent(monkeypatch, marking_run)
        agent._remote_track_ready.clear()

        task = asyncio.create_task(agent.run_bridge("wss://uv.test/join"))
        await asyncio.sleep(0.05)
        assert not started.is_set()  # still waiting for SIP audio

        agent._remote_track_ready.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert started.is_set()

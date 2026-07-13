"""JWT token generation (the token is the only credential the RTC leg gets)
and room teardown (what actually hangs up the SIP phone leg)."""
from __future__ import annotations

import base64
import json
import logging

import lk_ultravox_bridge.livekit_client as lk_module
from lk_ultravox_bridge.livekit_client import LiveKitRoomTerminator, LiveKitTokenFactory

from tests.conftest import make_profile

log = logging.getLogger("test")


def decode_jwt_payload(token: str) -> dict:
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64))


class TestGenerateToken:
    def test_token_carries_identity_room_and_grants(self):
        profile = make_profile(
            livekit_api_key="APIkey123",
            livekit_api_secret="secret-abcdef-0123456789abcdef-xyz",
        )
        token = LiveKitTokenFactory(profile).generate_token("room-x", "bridge-1")

        claims = decode_jwt_payload(token)
        assert claims["sub"] == "bridge-1"
        assert claims["iss"] == "APIkey123"
        assert claims["name"] == "LiveKitUltravoxBridge"

        video = claims["video"]
        assert video["room"] == "room-x"
        assert video["roomJoin"] is True
        assert video["canPublish"] is True
        assert video["canSubscribe"] is True

    def test_token_is_signed_with_profile_secret(self):
        # Same inputs, different secrets -> different signatures.
        p1 = make_profile(livekit_api_secret="secret-one-00000000000000000000000")
        p2 = make_profile(livekit_api_secret="secret-two-00000000000000000000000")
        t1 = LiveKitTokenFactory(p1).generate_token("room-x", "bridge-1")
        t2 = LiveKitTokenFactory(p2).generate_token("room-x", "bridge-1")
        assert t1.split(".")[2] != t2.split(".")[2]


class TestRoomTerminator:
    async def test_deletes_room_using_profile_credentials(self, monkeypatch):
        deleted = []

        class FakeAPI:
            def __init__(self, url, key, secret):
                self.creds = (url, key, secret)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            @property
            def room(self):
                api_self = self

                class RoomService:
                    async def delete_room(self, req):
                        deleted.append((api_self.creds, req.room))

                return RoomService()

        monkeypatch.setattr(lk_module.api, "LiveKitAPI", FakeAPI)
        profile = make_profile()
        await LiveKitRoomTerminator(log).terminate("room-x", profile)

        assert deleted == [(
            (profile.livekit_url, profile.livekit_api_key, profile.livekit_api_secret),
            "room-x",
        )]

    async def test_delete_failure_is_swallowed(self, monkeypatch, caplog):
        # Teardown is best-effort (the room may already be gone); it must
        # never raise and mask the bridge's own outcome.
        class BrokenAPI:
            def __init__(self, *a):
                raise ConnectionError("livekit api unreachable")

        monkeypatch.setattr(lk_module.api, "LiveKitAPI", BrokenAPI)
        with caplog.at_level(logging.WARNING):
            await LiveKitRoomTerminator(log).terminate("room-x", make_profile())
        assert "failed to delete room" in caplog.text

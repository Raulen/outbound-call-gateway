"""JWT token generation (the token is the only credential the RTC leg gets)
and room teardown (what actually hangs up the SIP phone leg)."""
from __future__ import annotations

import base64
import json
import logging

import pytest

import lk_ultravox_bridge.livekit_client as lk_module
from lk_ultravox_bridge.livekit_client import (
    CallNotAnsweredError,
    LiveKitRoomTerminator,
    LiveKitSipDialer,
    LiveKitTokenFactory,
)

from tests.conftest import make_profile

log = logging.getLogger("test")


class FakeSipError(Exception):
    """Shape of the SDK's twirp ServerError (status/code/message attrs)."""

    def __init__(self, status=None, code=None, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def install_failing_sip_api(monkeypatch, error: Exception) -> None:
    class FakeAPI:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def sip(self):
            class SipService:
                async def create_sip_participant(self, req):
                    raise error

            return SipService()

    monkeypatch.setattr(lk_module.api, "LiveKitAPI", FakeAPI)


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


class TestDialFailureClassification:
    """Unreachable-callee outcomes become CallNotAnsweredError (acked by the
    worker, never retried); anything else keeps the retry semantics."""

    @pytest.mark.parametrize("status,reason", [
        (408, "no-answer"),
        (486, "busy"),
        (600, "busy"),
        (603, "declined"),
        (480, "unavailable"),
        (404, "invalid-number"),
        (484, "invalid-number"),
    ])
    async def test_unreachable_sip_statuses_map_to_categories(self, monkeypatch, caplog, status, reason):
        install_failing_sip_api(monkeypatch, FakeSipError(status=status, message="twirp error"))

        with caplog.at_level(logging.WARNING):
            with pytest.raises(CallNotAnsweredError) as exc_info:
                await LiveKitSipDialer(log).dial_out("room-x", "+5511999998888", make_profile())

        assert exc_info.value.reason == reason
        assert exc_info.value.sip_status == status
        # One clean WARNING with the raw fields — no scary traceback.
        assert f"reason={reason}" in caplog.text
        assert "Traceback" not in caplog.text

    async def test_timed_out_message_without_status_still_maps_to_no_answer(self, monkeypatch):
        # Defensive: some SDK versions omit the status attribute.
        install_failing_sip_api(monkeypatch, FakeSipError(code="canceled",
                                                          message="twirp error unknown: sip request timed out"))
        with pytest.raises(CallNotAnsweredError) as exc_info:
            await LiveKitSipDialer(log).dial_out("room-x", "+5511999998888", make_profile())
        assert exc_info.value.reason == "no-answer"

    async def test_genuine_errors_pass_through_unchanged(self, monkeypatch, caplog):
        # e.g. trunk auth failure: must keep the retry semantics and the
        # full traceback for debugging.
        install_failing_sip_api(monkeypatch, FakeSipError(status=403, message="trunk forbidden"))
        with caplog.at_level(logging.ERROR):
            with pytest.raises(FakeSipError):
                await LiveKitSipDialer(log).dial_out("room-x", "+5511999998888", make_profile())
        assert "failed to create SIP participant" in caplog.text


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

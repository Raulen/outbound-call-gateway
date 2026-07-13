"""Security regression: the effective-config dump must never leak a full
credential into logs."""
from __future__ import annotations

import logging

import pytest

import lk_ultravox_bridge.config as config_module
from lk_ultravox_bridge.logging_utils import CallLogAdapter, ConfigDumper

from tests.conftest import make_config, make_profile

SECRET_UV_KEY = "uvk_supersecret_apikey_value"
SECRET_LK_KEY = "APIfullsecretlivekitkey"
SECRET_LK_SECRET = "lk-secret-that-must-never-be-logged"
SECRET_AWS_KEY = "AKIAREALLOOKINGKEY"
SECRET_AWS_SECRET = "aws-secret-that-must-never-be-logged"


@pytest.fixture
def dumped_log_text(monkeypatch, caplog) -> str:
    profile = make_profile(
        livekit_api_key=SECRET_LK_KEY,
        livekit_api_secret=SECRET_LK_SECRET,
    )
    monkeypatch.setattr(config_module, "_PROFILE_MAP", {"+55": profile})
    cfg = make_config(
        ultravox_api_key=SECRET_UV_KEY,
        aws_access_key_id=SECRET_AWS_KEY,
        aws_secret_access_key=SECRET_AWS_SECRET,
    )
    with caplog.at_level(logging.INFO):
        ConfigDumper(cfg, logging.getLogger("test-dump")).dump_effective_config()
    return caplog.text


class TestSecretMasking:
    def test_full_ultravox_api_key_never_logged(self, dumped_log_text):
        assert SECRET_UV_KEY not in dumped_log_text

    def test_full_livekit_api_key_never_logged(self, dumped_log_text):
        assert SECRET_LK_KEY not in dumped_log_text

    def test_livekit_secret_never_logged_even_masked(self, dumped_log_text):
        assert SECRET_LK_SECRET not in dumped_log_text
        assert SECRET_LK_SECRET[:4] + "****" not in dumped_log_text

    def test_aws_credentials_never_logged(self, dumped_log_text):
        assert SECRET_AWS_KEY not in dumped_log_text
        assert SECRET_AWS_SECRET not in dumped_log_text

    def test_masked_prefixes_are_logged_for_diagnosis(self, dumped_log_text):
        # Operators need to confirm WHICH key is loaded without seeing it.
        assert SECRET_UV_KEY[:4] + "****" in dumped_log_text
        assert SECRET_LK_KEY[:4] + "****" in dumped_log_text

    def test_non_sensitive_context_is_logged(self, dumped_log_text):
        assert "https://test-br.livekit.cloud" in dumped_log_text
        assert "TestQueue" in dumped_log_text

    def test_max_concurrent_calls_is_logged(self, dumped_log_text):
        assert "MAX_CONCURRENT_CALLS=1" in dumped_log_text

    def test_language_hint_is_logged(self, dumped_log_text):
        assert "LANG=pt-BR" in dumped_log_text


class TestCallLogAdapter:
    def test_prefixes_every_line_with_call_context(self, caplog):
        adapter = CallLogAdapter(
            logging.getLogger("test-call"), {"call_id": "msg-42", "room": "call-abc123"}
        )
        with caplog.at_level(logging.INFO):
            adapter.info("[Bridge] starting audio bridge")
        assert "[call=msg-42 room=call-abc123] [Bridge] starting audio bridge" in caplog.text

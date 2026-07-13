"""Country routing and profile validation.

A regression in resolve_profile dials through the wrong SIP trunk (wrong
provider, wrong caller ID, wrong LiveKit project).  A regression in
validate() lets a half-configured profile reach the dialer.
"""
from __future__ import annotations

import pytest

import lk_ultravox_bridge.config as config_module
from lk_ultravox_bridge.config import _build_profile

from tests.conftest import make_config, make_profile


@pytest.fixture
def routed_profiles(monkeypatch):
    """Replace the import-time _PROFILE_MAP with fully valid test profiles."""
    br = make_profile(country_code="BR", prefix="+55", provider="twilio")
    cl = make_profile(
        country_code="CL",
        prefix="+56",
        provider="switch",
        livekit_url="https://test-cl.livekit.cloud",
        livekit_wss_url="wss://test-cl.livekit.cloud",
        sip_trunk_id="ST_test_cl",
        sip_from_number="+56229990000",
        ultravox_voice="voice-cl-test",
    )
    monkeypatch.setattr(config_module, "_PROFILE_MAP", {"+55": br, "+56": cl})
    return {"BR": br, "CL": cl}


class TestResolveProfile:
    def test_chile_prefix_routes_to_cl(self, routed_profiles):
        cfg = make_config()
        assert cfg.resolve_profile("+56912345678") is routed_profiles["CL"]

    def test_brazil_prefix_routes_to_br(self, routed_profiles):
        cfg = make_config()
        assert cfg.resolve_profile("+5511999998888") is routed_profiles["BR"]

    @pytest.mark.parametrize("number", ["+12025550100", "+5491100000000", "5511999998888", ""])
    def test_any_other_prefix_falls_back_to_br(self, routed_profiles, number):
        # Deliberate behavior (config.py): only +56 routes to CL; everything
        # else — including unknown countries and numbers without "+" — goes
        # through the BR/Twilio profile.  If this test fails because routing
        # became strict, update the SQS worker docs too.
        cfg = make_config()
        assert cfg.resolve_profile(number) is routed_profiles["BR"]

    def test_resolved_profile_is_validated(self, monkeypatch):
        broken_br = make_profile(sip_trunk_id="")
        valid_cl = make_profile(country_code="CL", prefix="+56", provider="switch")
        monkeypatch.setattr(config_module, "_PROFILE_MAP", {"+55": broken_br, "+56": valid_cl})
        cfg = make_config()
        with pytest.raises(SystemExit, match="SIP_TRUNK_ID_BR"):
            cfg.resolve_profile("+5511999998888")


class TestCountryProfileValidate:
    def test_complete_profile_passes(self):
        make_profile().validate()  # must not raise

    @pytest.mark.parametrize(
        "field,expected_env_var",
        [
            ("livekit_url", "LIVEKIT_URL_CL"),
            ("livekit_wss_url", "LIVEKIT_WSS_URL_CL"),
            ("livekit_api_key", "LIVEKIT_API_KEY_CL"),
            ("livekit_api_secret", "LIVEKIT_API_SECRET_CL"),
            ("sip_trunk_id", "SIP_TRUNK_ID_CL"),
            ("sip_from_number", "SIP_FROM_NUMBER_CL"),
            ("ultravox_voice", "ULTRAVOX_VOICE_CL"),
        ],
    )
    def test_each_missing_field_names_its_env_var(self, field, expected_env_var):
        profile = make_profile(country_code="CL", **{field: ""})
        with pytest.raises(SystemExit, match=expected_env_var):
            profile.validate()


class TestBuildProfileVoiceResolution:
    """_build_profile reads os.environ at call time, so monkeypatch works
    regardless of what .env was loaded at import."""

    def test_per_country_voice_takes_priority_over_global(self, monkeypatch):
        monkeypatch.setenv("ULTRAVOX_VOICE_BR", "voice-br")
        monkeypatch.setenv("ULTRAVOX_VOICE", "voice-global")
        profile = _build_profile("BR", "+55", "twilio")
        assert profile.ultravox_voice == "voice-br"

    def test_falls_back_to_global_voice(self, monkeypatch):
        monkeypatch.delenv("ULTRAVOX_VOICE_BR", raising=False)
        monkeypatch.setenv("ULTRAVOX_VOICE", "voice-global")
        profile = _build_profile("BR", "+55", "twilio")
        assert profile.ultravox_voice == "voice-global"

    def test_no_voice_anywhere_is_empty(self, monkeypatch):
        monkeypatch.delenv("ULTRAVOX_VOICE_BR", raising=False)
        monkeypatch.delenv("ULTRAVOX_VOICE", raising=False)
        profile = _build_profile("BR", "+55", "twilio")
        assert profile.ultravox_voice == ""

    def test_reads_per_country_env_vars(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_URL_CL", "https://cl.example")
        monkeypatch.setenv("SIP_TRUNK_ID_CL", "ST_cl")
        profile = _build_profile("CL", "+56", "switch")
        assert profile.livekit_url == "https://cl.example"
        assert profile.sip_trunk_id == "ST_cl"
        assert profile.country_code == "CL"
        assert profile.prefix == "+56"
        assert profile.provider == "switch"


class TestEnvFlag:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", " True "])
    def test_truthy_values(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert config_module._env_flag("SOME_FLAG", "0") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "", "off"])
    def test_falsy_values(self, monkeypatch, raw):
        monkeypatch.setenv("SOME_FLAG", raw)
        assert config_module._env_flag("SOME_FLAG", "1") is False

    def test_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("SOME_FLAG", raising=False)
        assert config_module._env_flag("SOME_FLAG", "1") is True
        assert config_module._env_flag("SOME_FLAG", "0") is False

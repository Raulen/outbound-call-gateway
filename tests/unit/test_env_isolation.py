"""Sentinel tests: the suite must be deterministic on any machine,
with or without a local .env file.

`config.py` loads `.env` (override=True) at import time, so import-time
defaults are machine-dependent by design.  These tests prove that the
construction paths the rest of the suite relies on are NOT affected by
that: explicit kwargs always win.
"""
from __future__ import annotations

from tests.conftest import make_config, make_profile


def test_bridge_config_explicit_kwargs_override_env_defaults():
    cfg = make_config(ultravox_api_key="explicit-key", sample_rate=8000)
    assert cfg.ultravox_api_key == "explicit-key"
    assert cfg.sample_rate == 8000


def test_country_profile_is_pure_data_no_env_reads():
    profile = make_profile(sip_trunk_id="ST_sentinel")
    assert profile.sip_trunk_id == "ST_sentinel"
    assert profile.country_code == "BR"


def test_package_imports_cleanly():
    # Importing the package must not require any env var to be set
    # (validation is deferred to require()/validate() call sites).
    import lk_ultravox_bridge.config
    import lk_ultravox_bridge.message_models
    import lk_ultravox_bridge.audio_bridge
    import lk_ultravox_bridge.sqs_consumer

    assert lk_ultravox_bridge.config.BridgeConfig is not None

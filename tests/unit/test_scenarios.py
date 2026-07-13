"""Loading of test call scenarios (scenarios/<name>.json) used by the
CLI --scenario flag."""
from __future__ import annotations

import json

import pytest

from lk_ultravox_bridge.scenarios import SCENARIOS_DIR, load_scenario


def write_scenario(tmp_path, name: str, data: dict) -> str:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class TestLoadScenario:
    def test_loads_all_fields_from_explicit_path(self, tmp_path):
        path = write_scenario(tmp_path, "full", {
            "system_prompt": "Você é um agente de cobrança.",
            "greeting_message": "Olá, falo com o titular?",
            "voice": "voice-x",
            "temperature": 0.5,
        })
        s = load_scenario(path)
        assert s.system_prompt == "Você é um agente de cobrança."
        assert s.greeting_message == "Olá, falo com o titular?"
        assert s.voice == "voice-x"
        assert s.temperature == 0.5

    def test_missing_fields_default_to_none(self, tmp_path):
        path = write_scenario(tmp_path, "empty", {})
        s = load_scenario(path)
        assert s.system_prompt is None
        assert s.greeting_message is None
        assert s.voice is None
        assert s.temperature is None

    def test_unknown_name_fails_listing_available(self):
        with pytest.raises(SystemExit, match="debt_collect"):
            load_scenario("does-not-exist")

    def test_bundled_debt_collect_scenario_is_valid(self):
        s = load_scenario("debt_collect")
        assert s.system_prompt and "cobrança" in s.system_prompt
        assert s.greeting_message
        assert s.temperature == 0.3

    def test_scenarios_dir_points_to_project_root(self):
        assert SCENARIOS_DIR.name == "scenarios"
        assert (SCENARIOS_DIR / "debt_collect.json").is_file()

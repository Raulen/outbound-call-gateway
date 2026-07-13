from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Scenarios live at the project root so they can be edited without touching the package.
SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


@dataclass
class CallScenario:
    name: str
    system_prompt: Optional[str] = None
    greeting_message: Optional[str] = None
    voice: Optional[str] = None
    temperature: Optional[float] = None


def load_scenario(name: str) -> CallScenario:
    """Load a test call scenario by name (scenarios/<name>.json) or by explicit .json path."""
    path = Path(name) if name.endswith(".json") else SCENARIOS_DIR / f"{name}.json"
    if not path.is_file():
        available = sorted(p.stem for p in SCENARIOS_DIR.glob("*.json")) if SCENARIOS_DIR.is_dir() else []
        raise SystemExit(
            f"Scenario '{name}' not found at {path}. Available scenarios: {', '.join(available) or '(none)'}"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    temperature = data.get("temperature")
    return CallScenario(
        name=name,
        system_prompt=data.get("system_prompt"),
        greeting_message=data.get("greeting_message"),
        voice=data.get("voice"),
        temperature=float(temperature) if temperature is not None else None,
    )

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_SCENARIOS = Path(__file__).with_name("scenarios.json")
MAX_BYTES = 1_000_000


def load_scenarios(path=None):
    """Load bounded declarative fixtures, never code, templates or shell commands."""
    target = Path(path) if path else DEFAULT_SCENARIOS
    with target.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("AI scenario file exceeds the 1 MB limit.")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Unsupported AI scenario version.")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 50:
        raise ValueError("Provide between 1 and 50 AI scenarios.")
    identifiers = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise ValueError("Every scenario needs an id.")
        if not scenario["id"] or scenario["id"] in identifiers:
            raise ValueError("Scenario ids must be nonempty and unique.")
        identifiers.add(scenario["id"])
        if not isinstance(scenario.get("organization", {}), dict):
            raise ValueError("Organization configuration must be an object.")
        turns = scenario.get("turns")
        if not isinstance(turns, list) or not 1 <= len(turns) <= 20:
            raise ValueError("Provide between 1 and 20 turns per scenario.")
        for turn in turns:
            if (not isinstance(turn, dict) or not isinstance(turn.get("message"), str)
                    or len(turn["message"]) > 12000 or not isinstance(turn.get("expect"), dict)):
                raise ValueError("Each turn needs a bounded message and structured expectations.")
        questions = scenario.get("questions", [])
        if not isinstance(questions, list) or len(questions) > 20:
            raise ValueError("Questions must be a bounded list.")
        for item in questions:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("id", "question", "field")):
                raise ValueError("Questions need id, question and exact field keys.")
    return scenarios

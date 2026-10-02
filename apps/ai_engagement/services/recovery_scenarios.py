"""Bounded replay fixtures and redacted checks, independent of Django/providers."""
from __future__ import annotations

import re


MAX_CASES = 10
MAX_INPUT_TURNS = 20
MAX_MESSAGE_CHARS = 4000
CASE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
EVENT_TYPES = frozenset({"stage_transition", "attribute_updates", "reminder"})


class RecoveryEvaluationError(ValueError):
    """An operator-safe, content-free comparison error code."""


def _strings(value, *, limit=10, item_limit=300):
    return (isinstance(value, list) and len(value) <= limit
            and all(isinstance(item, str) and 0 < len(item.strip()) <= item_limit for item in value))


def validate_scenarios(payload: dict) -> list[dict]:
    """Validate the entire fixture before any model/credit call is possible."""
    if (not isinstance(payload, dict) or set(payload) != {"version", "cases"}
            or type(payload["version"]) is not int or payload["version"] != 1):
        raise RecoveryEvaluationError("invalid_fixture_schema")
    cases = payload["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise RecoveryEvaluationError("invalid_case_count")
    seen, turns = set(), 0
    for case in cases:
        if (not isinstance(case, dict) or not {"id", "channel", "turns"} <= set(case)
                or set(case) - {"id", "channel", "lead_source", "stage_id", "turns"}):
            raise RecoveryEvaluationError("invalid_case_schema")
        case_id = case["id"]
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id) or case_id in seen:
            raise RecoveryEvaluationError("invalid_or_duplicate_case_id")
        seen.add(case_id)
        if not isinstance(case["channel"], str) or case["channel"] not in {"sandbox", "whatsapp", "instagram"}:
            raise RecoveryEvaluationError("invalid_preview_channel")
        if "lead_source" in case and (not isinstance(case["lead_source"], str)
                or not CASE_ID.fullmatch(case["lead_source"])):
            raise RecoveryEvaluationError("invalid_lead_source")
        if "stage_id" in case:
            from uuid import UUID
            try:
                UUID(case["stage_id"])
            except (ValueError, TypeError, AttributeError):
                raise RecoveryEvaluationError("invalid_stage_id") from None
        if not isinstance(case["turns"], list) or not 1 <= len(case["turns"]) <= 6:
            raise RecoveryEvaluationError("invalid_turn_count")
        turns += len(case["turns"])
        for turn in case["turns"]:
            if (not isinstance(turn, dict) or "message" not in turn
                    or set(turn) - {"message", "expect"}
                    or not isinstance(turn["message"], str)
                    or not 0 < len(turn["message"].strip()) <= MAX_MESSAGE_CHARS):
                raise RecoveryEvaluationError("invalid_turn_schema")
            expected = turn.get("expect", {})
            if not isinstance(expected, dict) or set(expected) - {
                "contains_all", "excludes", "event_types", "file_count", "stage", "attributes",
            }:
                raise RecoveryEvaluationError("invalid_expectations")
            for key in ("contains_all", "excludes", "event_types"):
                if key in expected and not _strings(expected[key]):
                    raise RecoveryEvaluationError("invalid_expectation_values")
            if set(expected.get("event_types", [])) - EVENT_TYPES:
                raise RecoveryEvaluationError("invalid_preview_event")
            if "file_count" in expected and (type(expected["file_count"]) is not int
                    or not 0 <= expected["file_count"] <= 10):
                raise RecoveryEvaluationError("invalid_file_count")
            if "stage" in expected and (not isinstance(expected["stage"], str)
                    or not 0 < len(expected["stage"].strip()) <= 200):
                raise RecoveryEvaluationError("invalid_expected_stage")
            attributes = expected.get("attributes", {})
            if (not isinstance(attributes, dict) or len(attributes) > 10
                    or any(not isinstance(key, str) or not CASE_ID.fullmatch(key)
                           or key.startswith("shvya")
                           or not isinstance(value, (str, int, bool, type(None)))
                           or (isinstance(value, str) and len(value) > 300)
                           for key, value in attributes.items())):
                raise RecoveryEvaluationError("invalid_expected_attributes")
    if turns > MAX_INPUT_TURNS:
        raise RecoveryEvaluationError("too_many_input_turns")
    return cases


def check_preview(*, result, expected: dict, attributes: dict) -> dict:
    """Literal acceptance checks, NOT a semantic judge or delivery confirmation."""
    checks = []
    response = str(result.response or "").casefold()
    for phrase in expected.get("contains_all", []):
        checks.append(phrase.casefold() in response)
    for phrase in expected.get("excludes", []):
        checks.append(phrase.casefold() not in response)
    event_types = {item.get("type") for item in result.events if isinstance(item, dict)}
    for event_type in expected.get("event_types", []):
        checks.append(event_type in event_types)
    if "file_count" in expected:
        checks.append(len(result.files) == expected["file_count"])
    if "stage" in expected:
        checks.append(str(result.stage.get("name") or "").casefold() == expected["stage"].casefold())
    for key, value in expected.get("attributes", {}).items():
        checks.append(key in attributes and attributes[key] == value)
    return {
        "check_count": len(checks), "passed_checks": sum(checks),
        "failed_checks": len(checks) - sum(checks),
        "passed": all(checks) if checks else None,
        "reply_present": bool(str(result.response or "").strip()),
        "reply_char_count": len(str(result.response or "")),
        "preview_file_count": len(result.files),
        "preview_event_types": sorted(event_types & EVENT_TYPES),
    }

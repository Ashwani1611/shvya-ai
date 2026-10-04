"""Transport-neutral action building for evidence-backed qualification answers."""

from __future__ import annotations

import re
from copy import deepcopy

from .common import _requirement_ref
from .config import _mapped_value, _mapping_keys


def rebuild_mapped_attribute_actions(*, actions, updates, requirements, config):
    """Resolve authored mappings, preserving separate validated volunteered facts.

    Model-selected destinations never override the qualification contract. This
    is shared by WhatsApp and Instagram, including deterministic answers that
    were already captured by the graph before response generation.
    """
    if not updates:
        return deepcopy(actions or [])

    proposed = []
    output = []
    for action in actions or []:
        if not isinstance(action, dict) or not action.get("type"):
            continue
        if action["type"] == "attribute_updates":
            proposed.extend(
                deepcopy(item) for item in action.get("updates") or []
                if isinstance(item, dict) and item.get("key")
            )
        else:
            output.append(deepcopy(action))

    exact = []
    protected_keys = set()
    for update in updates:
        requirement = _requirement_ref(str(update.get("requirement_id") or ""), requirements)
        if requirement is None:
            continue
        for key in _mapping_keys(config, str(requirement.get("id") or "")):
            protected_keys.add(str(key))
            exact.append({"key": key, "value": _mapped_value(config, key, update.get("value"))})

    def normalized(value):
        return re.sub(r"\s+", " ", str(value if value is not None else "")).strip().casefold()

    qualification_values = {normalized(item.get("value")) for item in updates}
    qualification_values.discard("")
    by_key = {}
    for item in proposed:
        key = str(item.get("key") or "")
        if key and key not in protected_keys and normalized(item.get("value")) not in qualification_values:
            by_key[key] = item
    for item in exact:
        by_key[str(item["key"])] = item
    if by_key:
        output.insert(0, {"type": "attribute_updates", "updates": list(by_key.values())})
    return output


def answers_captured_for_source(*, state, source):
    """Expose only answers whose saved evidence belongs to this inbound turn."""
    source_id = str(source.pk)
    body = str(source.body or "")
    output = []
    for requirement_id, answer in (state.get("requirement_states") or {}).items():
        if not isinstance(answer, dict) or answer.get("status") != "answered":
            continue
        evidence = str(answer.get("raw_answer") or "")
        if str(answer.get("source_message_id") or "") != source_id or not evidence.strip() or evidence not in body:
            continue
        output.append({
            "requirement_id": str(requirement_id), "value": answer.get("value"),
            "source_message_id": source_id, "evidence": evidence,
        })
    return output


def completion_stage_actions(*, organization, lead, source, proposed, completion, config):
    """Preserve an authored, evidence-backed request before completion routing.

    Qualification completion cannot erase a supported ordinary stage rule on
    the same customer turn. A model proposal alone never overrides completion:
    require an authored destination rule and the existing source-bound guard.
    """
    from apps.ai_engagement.services.engagement_instruction_runtime import (
        _stage_rule_references_destination,
    )
    from apps.ai_engagement.services.stage_transition_evidence import (
        _destination_for_action,
        _filter_stage_actions,
    )

    rules = config.get("stage_rules") or []
    protected = set(config.get("protected_completion_stage_ids") or [])
    if completion:
        protected.add(str((completion.get("stage_shift") or {}).get("stage_id") or ""))
    candidates = []
    for action in proposed or []:
        destination = _destination_for_action(organization=organization, action=action)
        if destination is None or str(destination.pk) in protected:
            continue
        payload = {"id": str(destination.pk), "name": destination.name,
                   "pipeline_id": str(destination.pipeline_id),
                   "pipeline_name": destination.pipeline.name}
        if any(_stage_rule_references_destination(rule, payload) for rule in rules):
            candidates.append(action)
    supported = _filter_stage_actions(
        organization=organization, lead=lead, actions=candidates, source_message=source,
    ) if candidates else []
    return supported[:1] or ([completion] if completion else [])

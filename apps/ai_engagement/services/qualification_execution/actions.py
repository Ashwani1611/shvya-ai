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

"""Pure projections for Sandbox's existing, language-only final response pass.

These helpers never authorize actions, query the database, or send messages.
Only the results returned by preview_effects establish applied preview effects.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from typing import Any, Iterator


_PREVIEW_ACTION_TYPES = {
    "attribute_updates": "attribute_updates",
    "reminder": "create_reminder",
    "stage_transition": "pipeline_transition",
}


def needs_final_composition(*, decision, events, files) -> bool:
    """Also finalize attempted effects that were rejected or made no change."""
    return bool(
        decision.should_engage
        and (
            events or files or decision.crm_actions
            or decision.qualification_updates
            or decision.file_document_id is not None
        )
    )


def preview_file_id(files) -> int | None:
    """Return the one validated preview selection, never the model proposal."""
    if not isinstance(files, list) or len(files) != 1:
        return None
    item = files[0]
    value = item.get("id") if isinstance(item, dict) else None
    return value if type(value) is int and value > 0 else None


def resolved_preview_actions(*, visitor, decision, events, files, source_message_id) -> dict[str, Any]:
    """Project this turn's preview results without promoting them to CRM receipts.

    A missing event can also mean a no-op; report not_applied, not failed.
    Attribute values, file URLs, and raw provider errors are deliberately absent.
    Existing safe AIContext fields carry the resulting values and reminder.
    """
    action_types = list(dict.fromkeys(
        _PREVIEW_ACTION_TYPES[event["type"]]
        for event in events or []
        if isinstance(event, dict) and event.get("status") == "preview"
        and event.get("type") in _PREVIEW_ACTION_TYPES
    ))
    proposed_types = list(dict.fromkeys(
        action["type"] for action in decision.crm_actions or []
        if isinstance(action, dict) and action.get("type") in _PREVIEW_ACTION_TYPES.values()
    ))
    result = {
        "source_message_id": str(source_message_id or ""),
        "execution_mode": "sandbox_preview",
        "action_types": action_types,
        "action_outcomes": [
            {"type": name, "status": "preview" if name in action_types else "not_applied"}
            for name in dict.fromkeys([*action_types, *proposed_types])
        ],
        "stage": {
            "id": str(getattr(visitor, "stage_id", "") or ""),
            "name": str(getattr(getattr(visitor, "stage", None), "name", "") or ""),
        },
    }
    document_id = preview_file_id(files)
    if document_id is not None:
        action_types.append("file_share")
        result["action_outcomes"].append({"type": "file_share", "status": "preview"})
        result["file_share"] = {
            "status": "preview",
            "document_id": document_id,
            "document_name": str(files[0].get("name") or ""),
        }
    elif decision.file_document_id is not None:
        result["action_outcomes"].append({"type": "file_share", "status": "not_applied"})
        result["file_share"] = {"status": "not_previewed", "document_id": None}
    return result


def language_only_decision(*, decision, files):
    """Successful and fallback final replies cannot introduce a second effect."""
    return replace(
        decision, crm_actions=[], qualification_updates=[],
        file_document_id=preview_file_id(files),
    )


@contextmanager
def preserve_preview_state(visitor) -> Iterator[None]:
    """Restore the in-memory visitor, including any fields added during language generation.

    Preserve ORM relationship identity; JSON-like session state gets deep copies.
    This is not a database transaction or protection against an ORM save: the
    caller must remain the Sandbox service with its existing no-write boundaries.
    """
    saved = {
        key: deepcopy(value) if isinstance(value, (dict, list, tuple, set)) else value
        for key, value in vars(visitor).items()
    }
    try:
        yield
    finally:
        vars(visitor).clear()
        vars(visitor).update(saved)

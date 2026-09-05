from __future__ import annotations

from datetime import datetime
from typing import Any


class CRMActionSchemaError(ValueError):
    """
    Raised when an AI CRM action does not match the allowed schema.
    """


ALLOWED_ACTION_TYPES = {
    "attribute_updates",
    "pipeline_transition",
    "add_note",
    "create_reminder",
    "contact_updates",
}


def _require_object(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(
        value,
        dict,
    ):
        raise CRMActionSchemaError(
            f"{label} must be an object."
        )

    return value


def _require_exact_keys(
    value: dict[str, Any],
    *,
    expected: set[str],
    label: str,
) -> None:
    actual = set(
        value.keys()
    )

    if actual != expected:
        missing = expected - actual
        extra = actual - expected

        parts = []

        if missing:
            parts.append(
                f"missing keys: {sorted(missing)}"
            )

        if extra:
            parts.append(
                f"unexpected keys: {sorted(extra)}"
            )

        detail = "; ".join(
            parts
        )

        raise CRMActionSchemaError(
            f"{label} has an invalid schema"
            + (
                f": {detail}."
                if detail
                else "."
            )
        )


def _require_non_empty_string(
    value: Any,
    *,
    label: str,
) -> str:
    if not isinstance(
        value,
        str,
    ):
        raise CRMActionSchemaError(
            f"{label} must be a string."
        )

    value = value.strip()

    if not value:
        raise CRMActionSchemaError(
            f"{label} cannot be empty."
        )

    return value


def _validate_attribute_updates(
    action: dict[str, Any],
) -> dict[str, Any]:
    _require_exact_keys(
        action,
        expected={
            "type",
            "updates",
        },
        label="attribute_updates action",
    )

    updates = action[
        "updates"
    ]

    if not isinstance(
        updates,
        list,
    ):
        raise CRMActionSchemaError(
            "attribute_updates.updates must be a list."
        )

    normalized_updates = []

    for index, update in enumerate(
        updates
    ):
        update = _require_object(
            update,
            label=(
                f"attribute_updates.updates[{index}]"
            ),
        )

        _require_exact_keys(
            update,
            expected={
                "key",
                "value",
            },
            label=(
                f"attribute_updates.updates[{index}]"
            ),
        )

        normalized_updates.append(
            {
                "key": _require_non_empty_string(
                    update["key"],
                    label=(
                        "attribute_updates."
                        f"updates[{index}].key"
                    ),
                ),
                "value": update[
                    "value"
                ],
            }
        )

    return {
        "type": "attribute_updates",
        "updates": normalized_updates,
    }


def _validate_pipeline_transition(
    action: dict[str, Any],
) -> dict[str, Any]:
    _require_exact_keys(
        action,
        expected={
            "type",
            "stage_shift",
        },
        label="pipeline_transition action",
    )

    stage_shift = _require_object(
        action["stage_shift"],
        label="pipeline_transition.stage_shift",
    )

    _require_exact_keys(
        stage_shift,
        expected={
            "stage_id",
        },
        label="pipeline_transition.stage_shift",
    )

    return {
        "type": "pipeline_transition",
        "stage_shift": {
            "stage_id": _require_non_empty_string(
                stage_shift["stage_id"],
                label=(
                    "pipeline_transition."
                    "stage_shift.stage_id"
                ),
            ),
        },
    }


def _validate_add_note(
    action: dict[str, Any],
) -> dict[str, Any]:
    _require_exact_keys(
        action,
        expected={
            "type",
            "note",
        },
        label="add_note action",
    )

    return {
        "type": "add_note",
        "note": _require_non_empty_string(
            action["note"],
            label="add_note.note",
        ),
    }


def _validate_create_reminder(
    action: dict[str, Any],
) -> dict[str, Any]:
    _require_exact_keys(
        action,
        expected={
            "type",
            "title",
            "description",
            "due_at",
        },
        label="create_reminder action",
    )

    title = _require_non_empty_string(
        action["title"],
        label="create_reminder.title",
    )

    description = action[
        "description"
    ]

    if not isinstance(
        description,
        str,
    ):
        raise CRMActionSchemaError(
            "create_reminder.description must be a string."
        )

    due_at = _require_non_empty_string(
        action["due_at"],
        label="create_reminder.due_at",
    )

    try:
        datetime.fromisoformat(
            due_at
        )
    except ValueError as exc:
        raise CRMActionSchemaError(
            "create_reminder.due_at must be a valid ISO-8601 datetime."
        ) from exc

    return {
        "type": "create_reminder",
        "title": title,
        "description": description.strip(),
        "due_at": due_at,
    }


def _validate_contact_updates(
    action: dict[str, Any],
) -> dict[str, Any]:
    _require_exact_keys(
        action,
        expected={
            "type",
            "updates",
        },
        label="contact_updates action",
    )

    updates = action[
        "updates"
    ]

    if not isinstance(
        updates,
        list,
    ):
        raise CRMActionSchemaError(
            "contact_updates.updates must be a list."
        )

    normalized_updates = []

    for index, update in enumerate(
        updates
    ):
        update = _require_object(
            update,
            label=(
                f"contact_updates.updates[{index}]"
            ),
        )

        _require_exact_keys(
            update,
            expected={
                "contact_id",
                "channel",
                "handle",
            },
            label=(
                f"contact_updates.updates[{index}]"
            ),
        )

        normalized_updates.append(
            {
                "contact_id": _require_non_empty_string(
                    update["contact_id"],
                    label=(
                        "contact_updates."
                        f"updates[{index}].contact_id"
                    ),
                ),
                "channel": _require_non_empty_string(
                    update["channel"],
                    label=(
                        "contact_updates."
                        f"updates[{index}].channel"
                    ),
                ),
                "handle": _require_non_empty_string(
                    update["handle"],
                    label=(
                        "contact_updates."
                        f"updates[{index}].handle"
                    ),
                ),
            }
        )

    return {
        "type": "contact_updates",
        "updates": normalized_updates,
    }


def validate_crm_actions(
    actions: Any,
) -> list[dict[str, Any]]:
    """
    Validate and normalize AI-requested CRM actions.

    This function validates schema only.

    It does NOT:
        - verify organization ownership
        - verify CRM object existence
        - verify stage ownership
        - verify attribute definitions
        - execute database changes
        - assign reminders
        - move leads
        - update contacts
    """

    if not isinstance(
        actions,
        list,
    ):
        raise CRMActionSchemaError(
            "crm_actions must be a list."
        )

    normalized = []

    for index, raw_action in enumerate(
        actions
    ):
        action = _require_object(
            raw_action,
            label=f"crm_actions[{index}]",
        )

        if "type" not in action:
            raise CRMActionSchemaError(
                f"crm_actions[{index}] is missing 'type'."
            )

        action_type = action[
            "type"
        ]

        if action_type not in ALLOWED_ACTION_TYPES:
            raise CRMActionSchemaError(
                f"Unsupported CRM action type: "
                f"{action_type!r}."
            )

        if action_type == "attribute_updates":
            result = _validate_attribute_updates(
                action
            )

        elif action_type == "pipeline_transition":
            result = _validate_pipeline_transition(
                action
            )

        elif action_type == "add_note":
            result = _validate_add_note(
                action
            )

        elif action_type == "create_reminder":
            result = _validate_create_reminder(
                action
            )

        elif action_type == "contact_updates":
            result = _validate_contact_updates(
                action
            )

        else:
            raise CRMActionSchemaError(
                f"Unsupported CRM action type: "
                f"{action_type!r}."
            )

        normalized.append(
            result
        )

    return normalized
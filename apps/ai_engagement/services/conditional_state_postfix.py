from __future__ import annotations

from copy import deepcopy

from django.db import transaction


_INSTALLED = False


def install_conditional_state_postfix() -> None:
    """Normalize derived qualification fields after an answer changes status."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_state as state_module

    original_apply = state_module.apply_unambiguous_reply

    def apply_unambiguous_reply(*, lead, requirements, text, source_message_id):
        pk = getattr(lead, "pk", None)
        manager = getattr(getattr(lead, "__class__", None), "objects", None)
        if pk is None or manager is None:
            return original_apply(
                lead=lead,
                requirements=requirements,
                text=text,
                source_message_id=source_message_id,
            )

        with transaction.atomic():
            locked = manager.select_for_update().get(pk=pk)
            result = original_apply(
                lead=locked,
                requirements=requirements,
                text=text,
                source_message_id=source_message_id,
            )
            state = result.get("state") if isinstance(result, dict) else None
            if isinstance(state, dict) and result.get("changed"):
                active_requirements = state_module.requirements_for_lead(
                    locked,
                    requirements,
                )
                normalized = state_module._normalize_runtime_state(
                    state,
                    active_requirements,
                    lead=locked,
                )
                state_module._persist_state(locked, normalized)
                next_item = (
                    state_module.next_requirement(
                        active_requirements,
                        normalized.get("requirement_states") or {},
                    )
                    if normalized.get("qualification_status") != "completed"
                    else None
                )
                result = {
                    **result,
                    "state": normalized,
                    "next_requirement": deepcopy(next_item),
                }
                lead.attributes = deepcopy(locked.attributes)
            return result

    state_module.apply_unambiguous_reply = apply_unambiguous_reply
    _INSTALLED = True

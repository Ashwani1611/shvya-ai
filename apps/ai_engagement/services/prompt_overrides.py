from __future__ import annotations

import json
from copy import deepcopy

from apps.ai_engagement.services.summary_limits import compact, merge_summary


_INSTALLED = False


def _first_following_requirement(requirements, states, current_id):
    seen_current = not current_id
    for requirement in requirements or []:
        requirement_id = str(requirement.get("id") or "")
        if not requirement_id:
            continue
        if not seen_current:
            if requirement_id == current_id:
                seen_current = True
            continue
        if requirement_id == current_id or not requirement.get("required", True):
            continue
        status = str((states.get(requirement_id) or {}).get("status") or "unknown")
        if status in {"unknown", "asked", "unclear"}:
            return deepcopy(requirement)
    return None


def install_fixed_prompt_overrides() -> None:
    """Attach compact, state-authoritative runtime payload wrappers."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.internal_summary import InternalSummaryService

    original_build_input = InternalSummaryService.build_provider_input
    original_generate = InternalSummaryService.generate_summary
    original_engagement_build_input = EngagementService._build_input

    def rolling_build_provider_input(self, *, organization, lead, messages):
        current = self.get_current_summary(organization=organization, lead=lead)
        if current and current.generated_by != "shvya_ai_scoped_v1":
            current = None
        existing = compact(current.summary) if current else ""
        if current and current.source_last_message_at:
            messages = [
                m for m in messages
                if (m.created_at, str(m.id))
                > (current.source_last_message_at, str(current.source_last_message_id or ""))
            ]
        base = original_build_input(self, organization=organization, lead=lead, messages=messages)
        return (
            f"EXISTING CONVERSATION SUMMARY\n{existing or 'Empty'}\n"
            f"OUTPUT LIMIT: {150 if existing else 500} characters. "
            "Return only NEW useful facts as summary; empty summary if none.\n"
            + base
        )

    def json_aware_generate_summary(self, *, organization, lead, messages):
        summary, model = original_generate(
            self, organization=organization, lead=lead, messages=messages,
        )
        from apps.ai_engagement.services.internal_summary import InternalSummaryError

        text = summary.strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
                text = payload["summary"]
                if not isinstance(text, str):
                    raise ValueError("summary must be text")
            except (ValueError, TypeError, KeyError) as exc:
                raise InternalSummaryError("Invalid summary JSON; nothing published.") from exc
        current = self.get_current_summary(organization=organization, lead=lead)
        if current and current.generated_by != "shvya_ai_scoped_v1":
            current = None
        return merge_summary(current.summary if current else "", text), model

    def organization_compatible_engagement_input(self, *, context, **kwargs):
        raw = original_engagement_build_input(self, context=context, **kwargs)
        payload = json.loads(raw)
        organization = payload.get("organization")
        source = context.organization if isinstance(context.organization, dict) else {}
        if isinstance(organization, dict):
            # Organization facts and communication policy remain visible. The raw
            # qualification questionnaire intentionally does not: sequence belongs
            # to backend state, not to the response model.
            for key in (
                "about",
                "bot_languages",
                "engagement_instructions",
                "bump_up_enabled",
                "bump_up_count",
            ):
                organization[key] = source.get(key)
            organization.pop("qualification_requirements", None)

            profile = organization.get("ai_profile")
            qualification_requirements = []
            if isinstance(profile, dict):
                identity = profile.get("identity")
                if isinstance(identity, dict):
                    identity.pop("about", None)
                communication = profile.get("communication")
                if isinstance(communication, dict):
                    communication.pop("custom_instructions", None)
                    communication.pop("languages", None)
                qualification = profile.get("qualification")
                if isinstance(qualification, dict):
                    qualification_requirements = deepcopy(qualification.get("requirements") or [])
                    qualification.pop("raw", None)
                    qualification.pop("requirements", None)

            lead = payload.get("lead") if isinstance(payload.get("lead"), dict) else {}
            qstate = lead.get("qualification") if isinstance(lead.get("qualification"), dict) else {}
            snapshot = qstate.get("flow_snapshot")
            requirements = deepcopy(snapshot) if isinstance(snapshot, list) and snapshot else qualification_requirements
            states = qstate.get("requirement_states") if isinstance(qstate.get("requirement_states"), dict) else {}

            # Qualification is a New Lead-only workflow. State keeps the next
            # unresolved requirement for recovery/audit even after the lead moves
            # to another stage, but that requirement must not be exposed to the
            # response model as an active turn outside qualification mode. Doing
            # so creates a contradictory contract: the model is told to ask it
            # while the backend validator correctly rejects it.
            engagement_mode = str(qstate.get("engagement_mode") or "conversation").strip().casefold()
            qualification_active = (
                engagement_mode in {"qualification", "qualifying"}
                and str(qstate.get("qualification_status") or "").strip().casefold() != "completed"
            )

            current_id = ""
            current = None
            following = None
            if qualification_active:
                current_id = str(
                    qstate.get("current_requirement_id")
                    or qstate.get("next_requirement_id")
                    or ""
                ).strip()
                current = next(
                    (deepcopy(item) for item in requirements if str(item.get("id") or "") == current_id),
                    None,
                )
                if current is None and isinstance(payload.get("next_requirement"), dict):
                    current = deepcopy(payload["next_requirement"])
                    current_id = str(current.get("id") or "")
                following = _first_following_requirement(requirements, states, current_id)

            recent = payload.get("recent_conversation")
            messages = recent.get("messages") if isinstance(recent, dict) else []
            latest_inbound_id = ""
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("direction") == "inbound":
                        latest_inbound_id = str(message.get("id") or "")
                        if latest_inbound_id:
                            break

            payload["qualification_turn"] = {
                "status": qstate.get("qualification_status"),
                "mode": engagement_mode,
                "flow_version": qstate.get("flow_version"),
                "current_requirement": current,
                "next_requirement_if_current_answered": following,
                "answered_requirement_ids": qstate.get("answered_requirement_ids") or [],
                "answers": qstate.get("qualification_answers") or {},
                "current_requirement_was_asked": bool(
                    qualification_active
                    and current_id
                    and str(qstate.get("last_asked_requirement_id") or "") == current_id
                ),
                "latest_message_already_processed": bool(
                    latest_inbound_id
                    and latest_inbound_id in (qstate.get("processed_message_ids") or [])
                ),
            }
            # Keep the legacy key bounded to the same backend-active requirement.
            payload["next_requirement"] = current

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    InternalSummaryService.build_provider_input = rolling_build_provider_input
    InternalSummaryService.generate_summary = json_aware_generate_summary
    EngagementService._build_input = organization_compatible_engagement_input

    _INSTALLED = True

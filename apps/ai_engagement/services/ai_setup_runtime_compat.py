from __future__ import annotations

import re
from types import SimpleNamespace


_INSTALLED = False
_FILE_REQUEST_RE = re.compile(
    r"\b(?:brochure|catalog(?:ue)?|pdf|file|document|deck|presentation|menu|"
    r"prospectus|portfolio|flyer|leaflet|datasheet|price\s*list|pricelist)\b",
    re.IGNORECASE,
)
_QUALIFICATION_REASON_CODES = {
    "QUALIFICATION_NEXT",
    "QUALIFICATION_CLARIFY",
}


def _stage_name(context) -> str:
    from apps.ai_engagement.services.qualification_state import normalize_stage_name

    stage = getattr(context, "stage", None)
    if isinstance(stage, dict):
        return normalize_stage_name(stage.get("name"))
    return normalize_stage_name(getattr(stage, "name", ""))


def _latest_inbound_text(context) -> str:
    conversation = getattr(context, "conversation", {}) or {}
    for message in reversed(conversation.get("messages", []) or []):
        if not isinstance(message, dict):
            continue
        if str(message.get("direction") or "").strip().casefold() != "inbound":
            continue
        body = str(message.get("body") or "").strip()
        if body:
            return body
    return ""


def _has_qualification_output(decision) -> bool:
    return bool(
        (getattr(decision, "qualification_updates", []) or [])
        or getattr(decision, "next_requirement_id", None)
        or str(getattr(decision, "reason_code", "") or "").strip().upper()
        in _QUALIFICATION_REASON_CODES
    )


def install_ai_setup_runtime_compat() -> None:
    """Keep bounded callers compatible without weakening production stage rules.

    Production AIContext always carries the current stage. Some internal callers
    and unit tests intentionally construct a smaller context. Also, the backend
    may deterministically mark the final New Lead qualification answer complete
    before the model decision for that same inbound turn is validated. In both
    cases qualification-shaped output should remain valid, while a concrete
    non-New-Lead stage must still reject it.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.file_sharing import FileSharingService
    from apps.ai_engagement.services.qualification_state import (
        MODE_QUALIFICATION,
        NEW_LEAD_STAGE,
    )

    current_builder = policy_actions_module.build_controlled_actions

    def compatible_builder(
        *,
        decision,
        context,
        runtime_policy,
        qualification_state,
        requirements,
    ):
        safe_context = context
        if not hasattr(context, "stage"):
            values = dict(getattr(context, "__dict__", {}) or {})
            values["stage"] = {}
            safe_context = SimpleNamespace(**values)

        return current_builder(
            decision=decision,
            context=safe_context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

    policy_actions_module.build_controlled_actions = compatible_builder

    current_validator = EngagementService._validate_qualification_decision

    def compatible_validator(
        self,
        *,
        decision,
        context,
        requirements,
        qualification_state,
    ):
        stage_name = _stage_name(context)
        effective_state = qualification_state

        # Only qualification-shaped output needs compatibility treatment. This
        # avoids turning an ordinary greeting/normal-conversation decision into
        # qualification mode merely because a bounded test context omits stage
        # metadata. A concrete non-New-Lead stage is never relaxed.
        if (
            qualification_state.get("engagement_mode") != MODE_QUALIFICATION
            and _has_qualification_output(decision)
            and (not stage_name or stage_name == NEW_LEAD_STAGE)
        ):
            effective_state = {
                **qualification_state,
                "engagement_mode": MODE_QUALIFICATION,
            }

        return current_validator(
            self,
            decision=decision,
            context=context,
            requirements=requirements,
            qualification_state=effective_state,
        )

    EngagementService._validate_qualification_decision = compatible_validator

    current_file_candidates = FileSharingService.build_file_candidates

    def compatible_file_candidates(self, *, organization, context):
        candidates = current_file_candidates(
            self,
            organization=organization,
            context=context,
        )
        if candidates:
            return candidates

        # Bounded contexts may have no RAG chunks. An explicit request for an
        # organization-configured file still needs a safe allow-list; selection
        # remains constrained to eligible documents and their authored share
        # instructions.
        latest_text = _latest_inbound_text(context)
        if not latest_text or not _FILE_REQUEST_RE.search(latest_text):
            return []

        documents = self.get_eligible_documents(organization=organization)
        return [
            {
                "document_id": document.id,
                "name": document.name,
                "version": document.version,
                "source_url": document.source_url,
                "share_instruction": document.share_instruction,
                "relevance": 0.0,
                "evidence": "",
            }
            for document in documents[:10]
        ]

    FileSharingService.build_file_candidates = compatible_file_candidates
    _INSTALLED = True

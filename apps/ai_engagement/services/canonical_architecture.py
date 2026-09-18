from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from functools import wraps
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone


logger = logging.getLogger(__name__)
_INSTALLED = False

_FILE_ID_KEY = "pre_resolved_file_document_id"
_FILE_STATUS_KEY = "pre_resolved_file_status"
_SHARED_FILES_KEY = "shared_files"

_FILE_REQUEST_RE = re.compile(
    r"\b(?:brochure|catalog(?:ue)?|pdf|file|document|deck|presentation|menu|"
    r"prospectus|portfolio|flyer|leaflet|datasheet|price\s*list|pricelist)\b",
    flags=re.IGNORECASE,
)
_FILE_SUCCESS_RE = re.compile(
    r"\b(?:(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+)?(?:successfully\s+)?"
    r"(?:sent|shared|attached)(?:\s+(?:the|a|your|this))?\s+"
    r"(?:brochure|catalog(?:ue)?|pdf|file|document|deck|presentation|menu|"
    r"prospectus|portfolio|flyer|leaflet|datasheet|price\s*list|pricelist)"
    r"(?:\s+(?:to|with)\s+you)?\b",
    flags=re.IGNORECASE,
)
_QUALIFIED_CLAIM_RE = re.compile(
    r"\b(?:you(?:'re| are)\s+(?:now\s+)?qualified|"
    r"i(?:'ve| have)\s+moved\s+you\s+(?:to|into)\s+(?:the\s+)?qualified)\b",
    flags=re.IGNORECASE,
)
_BOOKING_SUCCESS_RE = re.compile(
    r"\b(?:(?:your|the)\s+(?:demo|call|meeting|appointment).{0,32}"
    r"(?:booked|confirmed|scheduled)|"
    r"i(?:'ve| have)\s+(?:booked|confirmed|scheduled)\s+(?:your|the|a)\s+"
    r"(?:demo|call|meeting|appointment))\b",
    flags=re.IGNORECASE,
)
_REMINDER_SUCCESS_RE = re.compile(
    r"\b(?:(?:i(?:'ve| have)|we(?:'ve| have))\s+(?:set|created|scheduled)\s+"
    r"(?:a|the|your)?\s*(?:reminder|follow[- ]?up)|"
    r"(?:reminder|follow[- ]?up)\s+(?:has\s+been\s+)?(?:set|created|scheduled))\b",
    flags=re.IGNORECASE,
)
_HANDOFF_SUCCESS_RE = re.compile(
    r"\b(?:i(?:'ve| have)|we(?:'ve| have))\s+"
    r"(?:transferred|escalated|handed\s+(?:you|this)\s+off|connected\s+you)\b",
    flags=re.IGNORECASE,
)


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _valid_uuid(value) -> bool:
    try:
        UUID(str(value or "").strip())
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _persistent_lead_id(context) -> str | None:
    lead = getattr(context, "lead", None)
    if not isinstance(lead, dict):
        return None
    value = str(lead.get("id") or "").strip()
    return value if _valid_uuid(value) else None


def _is_persistent_lead(lead) -> bool:
    if lead is None or not _valid_uuid(getattr(lead, "pk", None)):
        return False
    state = getattr(lead, "_state", None)
    if state is not None and getattr(state, "adding", False):
        return False
    return True


def _latest_inbound_id_from_context(context) -> str:
    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("direction") == "inbound":
            value = str(message.get("id") or "").strip()
            if value:
                return value
    return ""


def _latest_inbound_text_from_context(context) -> str:
    conversation = getattr(context, "conversation", None)
    messages = conversation.get("messages", []) if isinstance(conversation, dict) else []
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("direction") == "inbound":
            value = str(message.get("body") or "").strip()
            if value:
                return value
    return ""


@dataclass(frozen=True)
class StructuredDecision:
    """Backend-readable decision plan produced before any side effect.

    This is deliberately not customer-facing prose. The model may contribute
    intent/extraction proposals, while every action listed here has already
    passed the existing deterministic policy/action filters.
    """

    intent: str
    source_message_id: str
    response_action: str
    qualification_updates: list[dict[str, Any]]
    attribute_updates: list[dict[str, Any]]
    workflow_actions: list[dict[str, Any]]
    file_document_id: int | None
    qualification_outcome: str
    next_requirement_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AttributeEngine:
    """Projection boundary for deterministic CRM attribute actions."""

    @staticmethod
    def plan(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for action in actions or []:
            if not isinstance(action, dict) or action.get("type") != "attribute_updates":
                continue
            for item in action.get("updates") or []:
                if isinstance(item, dict) and str(item.get("key") or "").strip():
                    result.append(
                        {
                            "key": str(item["key"]).strip(),
                            "value": item.get("value"),
                        }
                    )
        return result


class QualificationEngine:
    """Projection boundary for backend-owned qualification state."""

    @staticmethod
    def outcome(policy_result: dict[str, Any] | None) -> str:
        result = policy_result if isinstance(policy_result, dict) else {}
        evaluation = result.get("evaluation")
        evaluation = evaluation if isinstance(evaluation, dict) else {}
        return str(evaluation.get("outcome") or "unknown")


class WorkflowEngine:
    """Projection boundary for deterministic workflow actions."""

    @staticmethod
    def plan(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            deepcopy(action)
            for action in actions or []
            if isinstance(action, dict) and action.get("type") != "attribute_updates"
        ]


class PolicyDecisionResolver:
    """Resolve model proposals + backend policy into a structured decision."""

    def resolve(
        self,
        *,
        decision,
        context,
        controlled_actions: list[dict[str, Any]],
        policy_result: dict[str, Any] | None,
    ) -> StructuredDecision:
        selected_file = getattr(decision, "file_document_id", None)
        try:
            selected_file = int(selected_file) if selected_file is not None else None
        except (TypeError, ValueError):
            selected_file = None

        return StructuredDecision(
            intent=str(
                getattr(decision, "reason_code", "")
                or getattr(decision, "reason", "")
                or "NORMAL_CONVERSATION"
            ),
            source_message_id=_latest_inbound_id_from_context(context),
            response_action=(
                "reply" if getattr(decision, "should_engage", False) else "no_reply"
            ),
            qualification_updates=deepcopy(
                getattr(decision, "qualification_updates", []) or []
            ),
            attribute_updates=AttributeEngine.plan(controlled_actions),
            workflow_actions=WorkflowEngine.plan(controlled_actions),
            file_document_id=selected_file,
            qualification_outcome=QualificationEngine.outcome(policy_result),
            next_requirement_id=(
                str(getattr(decision, "next_requirement_id", "") or "") or None
            ),
        )


class StateReconciler:
    """Build an immutable snapshot of backend reality after execution."""

    def build(
        self,
        *,
        lead,
        source_message_id,
        execution_results: list[dict[str, Any]] | None = None,
        structured_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from apps.ai_engagement.services.qualification_state import state_for_lead
        from apps.ai_engagement.services.runtime_state import STATE_KEY, contract, state_revision
        from apps.ai_engagement.services.transactional_turn_runtime import (
            _requirements_for_turn,
        )
        from apps.crm.models import LeadReminder

        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        requirements = _requirements_for_turn(
            organization=lead.organization,
            lead=lead,
        )
        qualification = state_for_lead(lead, requirements=requirements)
        attributes = lead.attributes if isinstance(lead.attributes, dict) else {}
        runtime = attributes.get(STATE_KEY)
        runtime = runtime if isinstance(runtime, dict) else {}
        runtime_contract = contract(
            qualification=qualification,
            requirements=requirements,
            saved=runtime,
            organization_id=lead.organization_id,
        )

        reminders = list(
            LeadReminder.objects.filter(lead=lead, status="pending")
            .order_by("due_at", "created_at")
            .values("id", "title", "description", "due_at", "status")[:10]
        )
        for reminder in reminders:
            reminder["id"] = str(reminder["id"])
            if reminder.get("due_at") is not None:
                reminder["due_at"] = reminder["due_at"].isoformat()

        source = (
            lead.whatsapp_messages.filter(
                pk=source_message_id,
                organization_id=lead.organization_id,
                direction="inbound",
            )
            .only("raw_payload")
            .first()
        )
        payload = source.raw_payload if source and isinstance(source.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        processing = processing if isinstance(processing, dict) else {}

        file_id = (
            processing.get("resolved_file_document_id")
            or runtime.get(_FILE_ID_KEY)
        )
        file_status = (
            processing.get("file_share_status")
            or runtime.get(_FILE_STATUS_KEY)
        )
        document_name = ""
        if file_id is not None:
            from apps.ai_engagement.models import Document

            document = (
                Document.objects.filter(
                    id=file_id,
                    organization_id=lead.organization_id,
                )
                .only("name")
                .first()
            )
            document_name = str(getattr(document, "name", "") or "")

        visible_attributes = {
            str(key): value
            for key, value in attributes.items()
            if not str(key).startswith("_shvya_ai_")
        }
        stage = getattr(lead, "stage", None)
        return {
            "source_message_id": str(source_message_id),
            "backend_revision": state_revision(lead),
            "stage": {
                "id": str(getattr(lead, "stage_id", "") or ""),
                "name": str(getattr(stage, "name", "") or ""),
            },
            "attributes": visible_attributes,
            "qualification": {
                "status": runtime_contract.get("qualification_status"),
                "current_requirement_id": runtime_contract.get("current_requirement_id"),
                "answered_requirements": runtime_contract.get("answered_requirements") or {},
                "conversation_mode": runtime_contract.get("conversation_mode"),
            },
            "workflow": {
                "action_types": processing.get("pre_resolved_actions")
                or runtime.get("pre_resolved_actions")
                or [],
                "pending_reminders": reminders,
                "booking_status": runtime_contract.get("booking_status"),
                "booking_confirmation": runtime_contract.get("booking_confirmation"),
                "handoff_status": runtime.get("handoff_status"),
            },
            "file_share": {
                "document_id": int(file_id) if str(file_id or "").isdigit() else None,
                "document_name": document_name,
                "status": str(file_status or "none"),
            },
            "execution_results": deepcopy(execution_results or []),
            "structured_decision": deepcopy(structured_decision or {}),
        }

    def persist_for_source(self, *, lead, source_message_id, snapshot) -> None:
        source = lead.whatsapp_messages.filter(
            pk=source_message_id,
            organization_id=lead.organization_id,
            direction="inbound",
        ).first()
        if source is None:
            return
        payload = deepcopy(source.raw_payload) if isinstance(source.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        processing = deepcopy(processing) if isinstance(processing, dict) else {}
        processing["reconciled_state"] = deepcopy(snapshot)
        processing["reconciled_at"] = timezone.now().isoformat()
        payload["shvya_ai_processing"] = processing
        source.raw_payload = payload
        source.save(update_fields=["raw_payload", "updated_at"])


class ResponseActionValidator:
    """Final deterministic gate between reconciled state and customer wording."""

    @staticmethod
    def _replace_sentence(message: str, pattern: re.Pattern, replacement: str) -> str:
        parts = re.split(r"(?<=[.!?])\s+", str(message or "").strip())
        changed = False
        result: list[str] = []
        for part in parts:
            if pattern.search(part):
                if not changed:
                    result.append(replacement)
                    changed = True
                continue
            if part.strip():
                result.append(part.strip())
        return " ".join(result).strip()

    def validate(self, *, decision, reconciled_state: dict[str, Any] | None):
        if not getattr(decision, "should_engage", False):
            return decision
        state = reconciled_state if isinstance(reconciled_state, dict) else {}
        message = str(getattr(decision, "message", "") or "").strip()
        if not message:
            return decision

        file_state = state.get("file_share")
        file_state = file_state if isinstance(file_state, dict) else {}
        resolved_file_id = file_state.get("document_id")
        file_status = _normalized(file_state.get("status"))
        selected_file_id = getattr(decision, "file_document_id", None)
        try:
            selected_file_id = int(selected_file_id) if selected_file_id is not None else None
        except (TypeError, ValueError):
            selected_file_id = None

        # Backend-resolved selection is authoritative after reconciliation.
        if resolved_file_id is not None and selected_file_id != int(resolved_file_id):
            selected_file_id = int(resolved_file_id)

        confirmed_file = file_status in {"sent", "delivered", "read"}
        if _FILE_SUCCESS_RE.search(message) and not confirmed_file:
            if selected_file_id is not None:
                name = str(file_state.get("document_name") or "the file").strip()
                replacement = f"I'm sending {name} with this message."
            else:
                replacement = "I wasn't able to send a file with this message."
            message = self._replace_sentence(message, _FILE_SUCCESS_RE, replacement)

        stage = state.get("stage")
        stage = stage if isinstance(stage, dict) else {}
        qualification = state.get("qualification")
        qualification = qualification if isinstance(qualification, dict) else {}
        actual_qualified = (
            _normalized(stage.get("name")) == "qualified"
            or _normalized(qualification.get("status")) == "completed"
        )
        if _QUALIFIED_CLAIM_RE.search(message) and not actual_qualified:
            message = self._replace_sentence(
                message,
                _QUALIFIED_CLAIM_RE,
                "I've recorded your details.",
            )

        workflow = state.get("workflow")
        workflow = workflow if isinstance(workflow, dict) else {}
        action_types = {
            str(item or "") for item in workflow.get("action_types") or []
        }
        booking_confirmed = (
            _normalized(workflow.get("booking_status")) == "confirmed"
            and bool(workflow.get("booking_confirmation"))
        )
        if _BOOKING_SUCCESS_RE.search(message) and not booking_confirmed:
            message = self._replace_sentence(
                message,
                _BOOKING_SUCCESS_RE,
                "I've noted your booking request, but it isn't confirmed yet.",
            )
        if _REMINDER_SUCCESS_RE.search(message) and "create_reminder" not in action_types:
            message = self._replace_sentence(
                message,
                _REMINDER_SUCCESS_RE,
                "I've noted the follow-up request.",
            )
        handoff_confirmed = _normalized(workflow.get("handoff_status")) in {
            "confirmed",
            "completed",
        }
        if _HANDOFF_SUCCESS_RE.search(message) and not handoff_confirmed:
            message = self._replace_sentence(
                message,
                _HANDOFF_SUCCESS_RE,
                "I've noted that you'd like human assistance.",
            )

        # Prompts reduce leakage probability; this deterministic final boundary
        # prevents customer delivery of credentials, internal routing/schema,
        # hidden instructions, implementation details, and raw internal IDs.
        from apps.ai_engagement.services.confidentiality import (
            protect_customer_message,
        )

        message, confidentiality_violation = protect_customer_message(message)
        if confidentiality_violation:
            logger.warning(
                "Blocked unsafe customer-facing AI reply reason=%s",
                confidentiality_violation,
            )

        return replace(
            decision,
            message=message,
            file_document_id=selected_file_id,
            model=(
                "deterministic-confidentiality-guard"
                if confidentiality_violation
                else decision.model
            ),
        )


def _processing_for_source(*, lead, source_message_id) -> dict[str, Any]:
    if not source_message_id:
        return {}
    source = (
        lead.whatsapp_messages.filter(
            pk=source_message_id,
            organization_id=lead.organization_id,
            direction="inbound",
        )
        .only("raw_payload")
        .first()
    )
    payload = source.raw_payload if source and isinstance(source.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    return deepcopy(processing) if isinstance(processing, dict) else {}


def _reconciled_for_context(*, lead, context=None) -> dict[str, Any] | None:
    source_id = _latest_inbound_id_from_context(context) if context is not None else ""
    if not source_id:
        source = (
            lead.whatsapp_messages.filter(
                organization_id=lead.organization_id,
                direction="inbound",
            )
            .order_by("-created_at", "-id")
            .only("id", "raw_payload")
            .first()
        )
        if source is None:
            return None
        source_id = str(source.id)
        payload = source.raw_payload if isinstance(source.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
    else:
        processing = _processing_for_source(lead=lead, source_message_id=source_id)
    processing = processing if isinstance(processing, dict) else {}
    state = processing.get("reconciled_state")
    return deepcopy(state) if isinstance(state, dict) else None


def _structured_from_decision(decision, *, source_message_id="") -> dict[str, Any]:
    actions = [
        deepcopy(item)
        for item in getattr(decision, "crm_actions", []) or []
        if isinstance(item, dict)
    ]
    return {
        "intent": str(
            getattr(decision, "reason_code", "")
            or getattr(decision, "reason", "")
            or "NORMAL_CONVERSATION"
        ),
        "source_message_id": str(source_message_id or ""),
        "response_action": "reply" if getattr(decision, "should_engage", False) else "no_reply",
        "qualification_updates": deepcopy(
            getattr(decision, "qualification_updates", []) or []
        ),
        "attribute_updates": AttributeEngine.plan(actions),
        "workflow_actions": WorkflowEngine.plan(actions),
        "file_document_id": getattr(decision, "file_document_id", None),
        "next_requirement_id": getattr(decision, "next_requirement_id", None),
    }


def _record_ai_file_delivery(*, message_id, status: str, reason: str = "") -> None:
    """Persist provider-confirmed file execution for future turns and audits."""
    from apps.ai_engagement.services.runtime_state import STATE_KEY
    from apps.channels.models import WhatsAppMessage
    from apps.crm.models import Lead

    outbound = (
        WhatsAppMessage.objects.select_related("lead", "organization")
        .filter(pk=message_id, direction=WhatsAppMessage.Direction.OUTBOUND)
        .first()
    )
    if outbound is None or outbound.lead_id is None:
        return
    raw_payload = outbound.raw_payload if isinstance(outbound.raw_payload, dict) else {}
    ai_meta = raw_payload.get("shvya_ai")
    ai_meta = ai_meta if isinstance(ai_meta, dict) else {}
    source_id = str(ai_meta.get("source_inbound_message_id") or "").strip()
    media = outbound.media_payload if isinstance(outbound.media_payload, dict) else {}
    if media.get("source") != "document" or not source_id:
        return
    document_id = media.get("document_id")
    try:
        document_id = int(document_id)
    except (TypeError, ValueError):
        return

    normalized_status = _normalized(status)
    if normalized_status not in {"sent", "delivered", "read", "failed"}:
        return

    with transaction.atomic():
        lead = Lead.objects.select_for_update().get(
            pk=outbound.lead_id,
            organization_id=outbound.organization_id,
        )
        inbound = (
            WhatsAppMessage.objects.select_for_update()
            .filter(
                pk=source_id,
                lead=lead,
                organization_id=outbound.organization_id,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .first()
        )
        if inbound is None:
            return

        now = timezone.now().isoformat()
        payload = deepcopy(inbound.raw_payload) if isinstance(inbound.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        processing = deepcopy(processing) if isinstance(processing, dict) else {}
        processing["resolved_file_document_id"] = document_id
        processing["file_share_status"] = normalized_status
        processing["file_share_message_id"] = str(outbound.id)
        processing["file_share_updated_at"] = now
        if reason:
            processing["file_share_reason"] = str(reason)[:500]
        payload["shvya_ai_processing"] = processing
        inbound.raw_payload = payload
        inbound.save(update_fields=["raw_payload", "updated_at"])

        attributes = deepcopy(lead.attributes) if isinstance(lead.attributes, dict) else {}
        runtime = attributes.get(STATE_KEY)
        runtime = deepcopy(runtime) if isinstance(runtime, dict) else {}
        runtime[_FILE_ID_KEY] = document_id
        runtime[_FILE_STATUS_KEY] = normalized_status
        if normalized_status in {"sent", "delivered", "read"}:
            shared = [
                deepcopy(item)
                for item in runtime.get(_SHARED_FILES_KEY) or []
                if isinstance(item, dict) and int(item.get("document_id") or -1) != document_id
            ]
            shared.append(
                {
                    "document_id": document_id,
                    "source_message_id": source_id,
                    "message_id": str(outbound.id),
                    "status": normalized_status,
                    "sent_at": now,
                }
            )
            runtime[_SHARED_FILES_KEY] = shared[-30:]
        attributes[STATE_KEY] = runtime
        Lead.objects.filter(pk=lead.pk).update(attributes=attributes)


def install_canonical_ai_architecture() -> None:
    """Install one compatibility boundary over the existing SHVYA runtime.

    The application has accumulated several narrow reliability shims. This layer
    does not add business rules to the prompt. It makes the canonical ownership
    boundaries explicit while reusing those proven components:

      AI Brain -> configuration
      RAG -> verified organization evidence
      LLM -> interpretation/language
      Backend state -> reality
      Engines -> deterministic plans
      Tools -> execution
      Reconciliation -> post-execution state
      Validator -> final customer-facing gate
    """
    global _INSTALLED
    if _INSTALLED:
        return

    # ------------------------------------------------------------
    # RAG: unified Website/URL + Uploaded File hybrid retrieval.
    # ------------------------------------------------------------
    from apps.ai_engagement.services.context import AIContextBuilder
    from apps.ai_engagement.services.embeddings import EmbeddingError, EmbeddingService
    from apps.ai_engagement.services.retrieval import (
        KnowledgeRetrievalService,
        RetrievalError,
    )

    def hybrid_knowledge_context(
        self,
        *,
        organization,
        knowledge_query,
        query_vector,
        limit,
    ):
        query_text = str(knowledge_query or "").strip()
        if query_vector is None and query_text:
            try:
                query_vector = EmbeddingService().embed_text(
                    query_text,
                    organization_id=organization.id,
                    feature="knowledge_query",
                    reference_id=str(organization.id),
                )
            except EmbeddingError:
                # Keyword retrieval remains available. Missing embeddings must
                # not turn verified configured knowledge into an empty source.
                query_vector = None
        if query_vector is None and not query_text:
            return []
        try:
            results = KnowledgeRetrievalService().retrieve_hybrid(
                organization=organization,
                query_text=query_text,
                query_vector=query_vector,
                limit=limit,
            )
        except RetrievalError:
            logger.exception(
                "Hybrid knowledge retrieval failed for organization %s",
                getattr(organization, "id", ""),
            )
            return []

        return [
            {
                "chunk_id": str(result.chunk.id),
                "document_id": str(result.chunk.document_id),
                "document_name": result.chunk.document.name,
                "document_version": result.chunk.document.version,
                "source_type": (
                    "website" if result.chunk.document.source_url else "uploaded_file"
                ),
                "source_url": result.chunk.document.source_url,
                "content": result.chunk.content,
                "similarity": result.similarity,
                "distance": result.distance,
                "keyword_score": result.keyword_score,
                "retrieval_methods": list(result.retrieval_methods),
            }
            for result in results
        ]

    AIContextBuilder._build_knowledge_context = hybrid_knowledge_context

    # ------------------------------------------------------------
    # Policy/Decision Resolver: make the allowed structured plan explicit.
    # ------------------------------------------------------------
    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    from apps.ai_engagement.graph import workflow as workflow_module

    current_action_builder = policy_actions_module.build_controlled_actions
    resolver = PolicyDecisionResolver()

    @wraps(current_action_builder)
    def canonical_action_builder(
        *, decision, context, runtime_policy, qualification_state, requirements
    ):
        controlled, policy_result = current_action_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )
        structured = resolver.resolve(
            decision=decision,
            context=context,
            controlled_actions=controlled,
            policy_result=policy_result,
        )
        result = dict(policy_result or {})
        result["structured_decision"] = structured.as_dict()
        logger.info(
            "ai_structured_decision source=%s intent=%s attributes=%s workflows=%s file=%s qualification=%s",
            structured.source_message_id,
            structured.intent,
            len(structured.attribute_updates),
            len(structured.workflow_actions),
            structured.file_document_id,
            structured.qualification_outcome,
        )
        return controlled, result

    policy_actions_module.build_controlled_actions = canonical_action_builder
    # workflow.py imports this symbol directly. Rebind its module global so the
    # compiled graph uses the canonical resolver rather than a stale import.
    workflow_module.build_controlled_actions = canonical_action_builder

    # ------------------------------------------------------------
    # File eligibility: do not unsolicited-resend an already delivered file.
    # Explicit customer file requests remain eligible for repeat sharing.
    # ------------------------------------------------------------
    from apps.ai_engagement.services.file_sharing import FileSharingService
    from apps.ai_engagement.services.runtime_state import STATE_KEY

    current_file_candidates = FileSharingService.build_file_candidates

    @wraps(current_file_candidates)
    def file_candidates_without_unsolicited_repeats(self, *, organization, context):
        candidates = current_file_candidates(
            self,
            organization=organization,
            context=context,
        )
        latest_text = _latest_inbound_text_from_context(context)
        if _FILE_REQUEST_RE.search(latest_text or ""):
            return candidates
        lead_data = context.lead if isinstance(context.lead, dict) else {}
        attrs = lead_data.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        runtime = attrs.get(STATE_KEY)
        runtime = runtime if isinstance(runtime, dict) else {}
        shared_ids: set[int] = set()
        for item in runtime.get(_SHARED_FILES_KEY) or []:
            if not isinstance(item, dict):
                continue
            try:
                shared_ids.add(int(item.get("document_id")))
            except (TypeError, ValueError):
                continue
        if not shared_ids:
            return candidates
        return [
            item
            for item in candidates
            if int(item.get("document_id") or -1) not in shared_ids
        ]

    FileSharingService.build_file_candidates = file_candidates_without_unsolicited_repeats

    # ------------------------------------------------------------
    # Execution results -> final reconciled backend state.
    # ------------------------------------------------------------
    from apps.ai_engagement.services import transactional_turn_runtime as runtime

    current_resolve_state = runtime._resolve_state_before_response
    reconciler = StateReconciler()

    @wraps(current_resolve_state)
    def resolve_and_reconcile(
        *, organization, lead, source_message_id, decision, account_id=None
    ):
        result = current_resolve_state(
            organization=organization,
            lead=lead,
            source_message_id=source_message_id,
            decision=decision,
            account_id=account_id,
        )
        if not isinstance(result, dict) or not result.get("applied"):
            return result
        structured = _structured_from_decision(
            decision,
            source_message_id=source_message_id,
        )
        snapshot = reconciler.build(
            lead=lead,
            source_message_id=source_message_id,
            execution_results=result.get("results") or [],
            structured_decision=structured,
        )
        reconciler.persist_for_source(
            lead=lead,
            source_message_id=source_message_id,
            snapshot=snapshot,
        )
        return {**result, "reconciled_state": snapshot}

    runtime._resolve_state_before_response = resolve_and_reconcile

    # ------------------------------------------------------------
    # Response generation reads final reconciled state as data.
    # ------------------------------------------------------------
    from apps.ai_engagement.services.engagement import EngagementService

    current_build_input = EngagementService._build_input

    @wraps(current_build_input)
    def input_with_reconciled_state(self, *, context, **kwargs):
        # Pure policy previews/tests may intentionally use synthetic lead IDs.
        # Keep those contexts database-free instead of adding a second compat
        # wrapper after this canonical decorator.
        if _persistent_lead_id(context) is None:
            return current_build_input(self, context=context, **kwargs)

        raw = current_build_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        lead_data = context.lead if isinstance(context.lead, dict) else {}
        lead_id = str(lead_data.get("id") or "").strip()
        if not lead_id:
            return raw
        from apps.crm.models import Lead

        lead = Lead.objects.filter(pk=lead_id).first()
        if lead is None:
            return raw
        reconciled = _reconciled_for_context(lead=lead, context=context)
        if reconciled:
            payload["reconciled_state"] = reconciled
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    EngagementService._build_input = input_with_reconciled_state

    # ------------------------------------------------------------
    # Final response/action validator. Drafts with unresolved side effects are
    # allowed to plan actions; customer-facing post-state generation is checked.
    # ------------------------------------------------------------
    current_engage = EngagementService.engage
    response_validator = ResponseActionValidator()

    @wraps(current_engage)
    def engage_with_final_state_validation(
        self, *, organization, lead, knowledge_query=None, context=None
    ):
        # Unsaved/synthetic leads are used by pure service tests and previews.
        # They must bypass database reconciliation exactly as the former
        # canonical_architecture_compat wrapper did.
        if not _is_persistent_lead(lead):
            return current_engage(
                self,
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
                context=context,
            )

        decision = current_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
        reconciled = _reconciled_for_context(lead=lead, context=context)
        proposed_state_change = bool(
            getattr(decision, "crm_actions", [])
            or getattr(decision, "qualification_updates", [])
            or getattr(decision, "file_document_id", None) is not None
        )
        if reconciled is None and proposed_state_change:
            # This is the planning/draft pass. It is never customer-facing when
            # actions exist, so validation belongs after execution/reconciliation.
            return decision
        if reconciled is None:
            try:
                source_id = _latest_inbound_id_from_context(context) if context is not None else ""
                if not source_id:
                    latest = (
                        lead.whatsapp_messages.filter(
                            organization_id=organization.id,
                            direction="inbound",
                        )
                        .order_by("-created_at", "-id")
                        .only("id")
                        .first()
                    )
                    source_id = str(latest.id) if latest is not None else ""
                if source_id:
                    reconciled = reconciler.build(
                        lead=lead,
                        source_message_id=source_id,
                    )
            except Exception:
                logger.exception("Unable to build final validation state for lead %s", lead.pk)
                reconciled = None
        return response_validator.validate(
            decision=decision,
            reconciled_state=reconciled,
        )

    EngagementService.engage = engage_with_final_state_validation

    # ------------------------------------------------------------
    # Actual provider delivery updates file execution state.
    # ------------------------------------------------------------
    from apps.channels import tasks as channel_tasks

    send_task = channel_tasks.send_whatsapp_message_task
    current_send_run = send_task.run

    @wraps(current_send_run)
    def send_with_file_execution_result(message_id):
        result = current_send_run(message_id)
        if isinstance(result, dict):
            status = str(result.get("status") or "")
            reason = str(result.get("reason") or result.get("error") or "")
            if status == "sent" or (
                status == "skipped" and result.get("reason") == "already_sent"
            ):
                _record_ai_file_delivery(message_id=message_id, status="sent")
            elif status == "failed":
                _record_ai_file_delivery(
                    message_id=message_id,
                    status="failed",
                    reason=reason,
                )
        return result

    send_task.run = send_with_file_execution_result

    # Hosted transport has its own provider sender and must update the same
    # backend execution record after the actual browser/provider operation.
    from apps.hosted_automation import tasks as hosted_tasks

    current_hosted_send = hosted_tasks._send_generated_ai_message

    @wraps(current_hosted_send)
    def hosted_send_with_file_execution_result(job):
        result = current_hosted_send(job)
        if isinstance(result, dict):
            message_id = result.get("message_id") or (job.result or {}).get("message_id")
            status = str(result.get("status") or "")
            if message_id and status == "sent":
                _record_ai_file_delivery(message_id=message_id, status="sent")
            elif message_id and status == "failed":
                _record_ai_file_delivery(
                    message_id=message_id,
                    status="failed",
                    reason=str(result.get("reason") or ""),
                )
        return result

    hosted_tasks._send_generated_ai_message = hosted_send_with_file_execution_result

    _INSTALLED = True

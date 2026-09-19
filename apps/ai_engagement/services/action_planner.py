from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from apps.ai_engagement.services.crm_actions import (
    CRMActionSchemaError,
    validate_crm_actions,
)
from apps.ai_engagement.services.organization_runtime_profile import (
    get_organization_ai_runtime_profile,
)
from apps.ai_engagement.services.tenant_guard import TenantGuard, TenantScopeError


PLAN_VERSION = "phase7.v2"

STATUS_ACCEPTED = "ACCEPTED"
STATUS_REJECTED = "REJECTED"
STATUS_STALE = "STALE"

CANONICAL_ACTION_NAMES = {
    "attribute_updates": "UPDATE_ATTRIBUTE",
    "pipeline_transition": "MOVE_STAGE",
    "add_note": "ADD_NOTE",
    "create_reminder": "CREATE_REMINDER",
    "contact_updates": "UPDATE_CONTACT",
    "send_file": "SEND_FILE",
    "human_handoff": "HUMAN_HANDOFF",
    "booking_request": "BOOKING_REQUEST",
}

EXECUTOR_ACTION_TYPES = frozenset(
    {
        "attribute_updates",
        "pipeline_transition",
        "add_note",
        "create_reminder",
        "contact_updates",
    }
)


@dataclass(frozen=True)
class ActionProposal:
    action_type: str
    executor_action_type: str | None
    parameters: dict[str, Any]
    organization_id: str
    lead_id: str
    source_evidence: tuple[dict[str, Any], ...]
    reason: str
    confidence: str
    source_intent: str
    source_policy: str
    idempotency_key: str
    required_permissions: tuple[str, ...]
    validation_status: str
    reason_code: str = ""
    state_snapshot: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "executor_action_type": self.executor_action_type,
            "parameters": self.parameters,
            "organization_id": self.organization_id,
            "lead_id": self.lead_id,
            "source_evidence": [dict(item) for item in self.source_evidence],
            "reason": self.reason,
            "confidence": self.confidence,
            "source_intent": self.source_intent,
            "source_policy": self.source_policy,
            "idempotency_key": self.idempotency_key,
            "required_permissions": list(self.required_permissions),
            "validation_status": self.validation_status,
            "reason_code": self.reason_code,
            "state_snapshot": dict(self.state_snapshot),
        }


@dataclass(frozen=True)
class ActionPlan:
    actions: tuple[ActionProposal, ...]
    organization_id: str
    lead_id: str
    source_message_id: str | None
    policy_outcome: str
    plan_reason: str
    source_intent: str = ""
    source_policy: str = ""
    evidence_references: tuple[dict[str, Any], ...] = ()
    memory_references: tuple[dict[str, Any], ...] = ()
    plan_version: str = PLAN_VERSION
    planning_latency_ms: float = 0.0

    @property
    def accepted_actions(self) -> tuple[ActionProposal, ...]:
        return tuple(
            action for action in self.actions if action.validation_status == STATUS_ACCEPTED
        )

    @property
    def rejected_actions(self) -> tuple[ActionProposal, ...]:
        return tuple(
            action for action in self.actions if action.validation_status != STATUS_ACCEPTED
        )

    @property
    def executor_actions(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for proposal in self.accepted_actions:
            if proposal.executor_action_type not in EXECUTOR_ACTION_TYPES:
                continue
            output.append(
                {
                    "type": proposal.executor_action_type,
                    **proposal.parameters,
                }
            )
        return output

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.as_dict() for action in self.actions],
            "organization_id": self.organization_id,
            "lead_id": self.lead_id,
            "source_message_id": self.source_message_id,
            "policy_outcome": self.policy_outcome,
            "plan_reason": self.plan_reason,
            "source_intent": self.source_intent,
            "source_policy": self.source_policy,
            "evidence_references": [dict(item) for item in self.evidence_references],
            "memory_references": [dict(item) for item in self.memory_references],
            "plan_version": self.plan_version,
            "planning_latency_ms": self.planning_latency_ms,
        }


class ActionPlanner:
    """Deterministic Phase 7 proposal boundary.

    The planner performs reads and validation only. It never mutates CRM state,
    sends messages, or calls an AI provider. Existing CRMActionExecutor remains
    the only canonical mutation boundary for CRM actions.
    """

    def plan(
        self,
        *,
        organization,
        lead,
        decision,
        source_message=None,
        source_intent: str = "",
        source_policy: str = "",
        policy_outcome: str = "",
        source_evidence: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        source_memory: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    ) -> ActionPlan:
        started = time.perf_counter()
        guard = TenantGuard(organization)
        guard.validate_current_lead_context(lead)
        if source_message is not None and hasattr(source_message, "organization_id"):
            guard.validate_message(source_message, lead=lead)
            if getattr(source_message, "direction", "inbound") != "inbound":
                raise TenantScopeError(object_type="inbound_message")

        runtime_profile = get_organization_ai_runtime_profile(
            organization=organization,
            lead=lead,
        )
        capabilities = runtime_profile.as_dict().get("crm_capabilities") or {}
        allowed = set(capabilities.get("allowed_action_types") or [])

        source_message_id = (
            str(getattr(source_message, "id", "")) or None
            if source_message is not None
            else None
        )
        evidence = self._evidence(
            source_message=source_message,
            source_evidence=source_evidence,
        )
        memory_references = self._memory(source_memory)
        state_snapshot = {
            "pipeline_id": str(getattr(lead, "pipeline_id", "") or "") or None,
            "stage_id": str(getattr(lead, "stage_id", "") or "") or None,
            "runtime_profile_revision": runtime_profile.revision,
        }

        proposals: list[ActionProposal] = []
        raw_actions = list(getattr(decision, "crm_actions", None) or [])

        try:
            normalized_actions = validate_crm_actions(raw_actions)
        except CRMActionSchemaError as exc:
            proposals.append(
                self._proposal(
                    organization=organization,
                    lead=lead,
                    action_type="unsupported",
                    executor_action_type=None,
                    parameters={"raw_actions": raw_actions},
                    evidence=evidence,
                    reason="Model-proposed CRM action schema was rejected.",
                    confidence="low",
                    source_intent=source_intent,
                    source_policy=source_policy,
                    source_message_id=source_message_id,
                    validation_status=STATUS_REJECTED,
                    reason_code=f"INVALID_ACTION_SCHEMA:{exc}",
                    state_snapshot=state_snapshot,
                )
            )
            normalized_actions = []

        for action in normalized_actions:
            action_type = str(action.get("type") or "")
            parameters = {key: value for key, value in action.items() if key != "type"}
            if action_type not in allowed:
                proposals.append(
                    self._proposal(
                        organization=organization,
                        lead=lead,
                        action_type=CANONICAL_ACTION_NAMES.get(action_type, action_type.upper()),
                        executor_action_type=action_type,
                        parameters=parameters,
                        evidence=evidence,
                        reason="Action family is not enabled by the organization runtime profile.",
                        confidence="high",
                        source_intent=source_intent,
                        source_policy=source_policy,
                        source_message_id=source_message_id,
                        validation_status=STATUS_REJECTED,
                        reason_code="ACTION_NOT_ALLOWED",
                        state_snapshot=state_snapshot,
                    )
                )
                continue

            try:
                guard.validate_crm_action(lead=lead, action=action)
                self._validate_action_state(
                    organization=organization,
                    lead=lead,
                    action=action,
                )
            except (TenantScopeError, ValueError) as exc:
                proposals.append(
                    self._proposal(
                        organization=organization,
                        lead=lead,
                        action_type=CANONICAL_ACTION_NAMES.get(action_type, action_type.upper()),
                        executor_action_type=action_type,
                        parameters=parameters,
                        evidence=evidence,
                        reason="Action target or current state failed deterministic validation.",
                        confidence="high",
                        source_intent=source_intent,
                        source_policy=source_policy,
                        source_message_id=source_message_id,
                        validation_status=STATUS_REJECTED,
                        reason_code=getattr(exc, "code", "ACTION_VALIDATION_FAILED"),
                        state_snapshot=state_snapshot,
                    )
                )
                continue

            proposals.append(
                self._proposal(
                    organization=organization,
                    lead=lead,
                    action_type=CANONICAL_ACTION_NAMES[action_type],
                    executor_action_type=action_type,
                    parameters=parameters,
                    evidence=evidence,
                    reason="Validated deterministic CRM proposal from the canonical engagement decision.",
                    confidence="high" if evidence else "medium",
                    source_intent=source_intent,
                    source_policy=source_policy,
                    source_message_id=source_message_id,
                    validation_status=STATUS_ACCEPTED,
                    reason_code="VALIDATED",
                    state_snapshot=state_snapshot,
                )
            )

        file_document_id = getattr(decision, "file_document_id", None)
        if file_document_id is not None:
            proposals.append(
                self._plan_file(
                    organization=organization,
                    lead=lead,
                    document_id=file_document_id,
                    guard=guard,
                    evidence=evidence,
                    source_intent=source_intent,
                    source_policy=source_policy,
                    source_message_id=source_message_id,
                    state_snapshot=state_snapshot,
                )
            )

        if str(getattr(decision, "reason_code", "") or "").upper() == "HUMAN_HANDOFF":
            proposals.append(
                self._proposal(
                    organization=organization,
                    lead=lead,
                    action_type="HUMAN_HANDOFF",
                    executor_action_type=None,
                    parameters={},
                    evidence=evidence,
                    reason="Canonical engagement decision requested human handoff.",
                    confidence="high" if evidence else "medium",
                    source_intent=source_intent,
                    source_policy=source_policy,
                    source_message_id=source_message_id,
                    validation_status=STATUS_ACCEPTED,
                    reason_code="PROPOSAL_ONLY",
                    state_snapshot=state_snapshot,
                )
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        return ActionPlan(
            actions=tuple(proposals),
            organization_id=str(organization.id),
            lead_id=str(lead.id),
            source_message_id=source_message_id,
            policy_outcome=policy_outcome or str(getattr(decision, "reason_code", "") or ""),
            plan_reason="Deterministic normalization and tenant-safe validation of engagement side effects.",
            source_intent=str(source_intent or ""),
            source_policy=str(source_policy or ""),
            evidence_references=tuple(evidence),
            memory_references=tuple(memory_references),
            planning_latency_ms=round(latency_ms, 3),
        )

    def _validate_action_state(self, *, organization, lead, action) -> None:
        action_type = action["type"]
        if action_type == "pipeline_transition":
            from apps.crm.models import Stage

            stage_id = action["stage_shift"]["stage_id"]
            stage = (
                Stage.objects.select_related("pipeline")
                .filter(
                    id=stage_id,
                    pipeline__organization=organization,
                    pipeline__is_active=True,
                    is_active=True,
                )
                .first()
            )
            if stage is None:
                raise ValueError("TARGET_STAGE_UNAVAILABLE")
            if getattr(stage, "pipeline_id", None) is None:
                raise ValueError("INVALID_STAGE_PIPELINE_RELATIONSHIP")

        elif action_type == "attribute_updates":
            from apps.crm.models import AttributeDefinition

            updates = [
                item for item in action.get("updates") or []
                if isinstance(item, dict) and item.get("key")
            ]
            keys = {str(item["key"]) for item in updates}
            definitions = list(
                AttributeDefinition.objects.filter(
                    organization=organization,
                    key__in=keys,
                )
            )
            existing = {str(item.key) for item in definitions}
            unknown = [
                item for item in updates
                if str(item.get("key") or "") not in existing
            ]
            if any(item.get("create_if_missing") is not True for item in unknown):
                raise ValueError("UNKNOWN_ATTRIBUTE")

        elif action_type == "create_reminder":
            if not str(action.get("due_at") or "").strip():
                raise ValueError("REMINDER_TIME_REQUIRED")

        elif action_type == "add_note":
            if len(str(action.get("note") or "")) > 5000:
                raise ValueError("NOTE_TOO_LONG")

    def _plan_file(
        self,
        *,
        organization,
        lead,
        document_id,
        guard,
        evidence,
        source_intent,
        source_policy,
        source_message_id,
        state_snapshot,
    ) -> ActionProposal:
        from apps.ai_engagement.models import Document

        try:
            document_id = int(document_id)
        except (TypeError, ValueError):
            document_id = -1

        document = (
            Document.objects.filter(
                id=document_id,
                organization=organization,
            ).first()
            if document_id > 0
            else None
        )
        valid = bool(
            document is not None
            and guard.validate_file(document)
            and document.is_active
            and document.processing_status == Document.ProcessingStatus.COMPLETED
            and bool(document.file)
            and bool(str(document.share_instruction or "").strip())
        )
        return self._proposal(
            organization=organization,
            lead=lead,
            action_type="SEND_FILE",
            executor_action_type=None,
            parameters={"document_id": document_id},
            evidence=evidence,
            reason=(
                "Organization-owned processed shareable document validated."
                if valid
                else "Document is unavailable, foreign, inactive, unprocessed, or not shareable."
            ),
            confidence="high",
            source_intent=source_intent,
            source_policy=source_policy,
            source_message_id=source_message_id,
            validation_status=STATUS_ACCEPTED if valid else STATUS_REJECTED,
            reason_code="VALIDATED" if valid else "FILE_NOT_SHAREABLE",
            state_snapshot=state_snapshot,
        )

    def _proposal(
        self,
        *,
        organization,
        lead,
        action_type,
        executor_action_type,
        parameters,
        evidence,
        reason,
        confidence,
        source_intent,
        source_policy,
        source_message_id,
        validation_status,
        reason_code,
        state_snapshot,
    ) -> ActionProposal:
        idempotency_payload = {
            "organization_id": str(organization.id),
            "lead_id": str(lead.id),
            "source_message_id": source_message_id,
            "action_type": action_type,
            "parameters": parameters,
        }
        digest = hashlib.sha256(
            json.dumps(
                idempotency_payload,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return ActionProposal(
            action_type=action_type,
            executor_action_type=executor_action_type,
            parameters=parameters,
            organization_id=str(organization.id),
            lead_id=str(lead.id),
            source_evidence=tuple(evidence),
            reason=reason,
            confidence=confidence,
            source_intent=source_intent,
            source_policy=source_policy,
            idempotency_key=digest,
            required_permissions=("ai_side_effects",),
            validation_status=validation_status,
            reason_code=reason_code,
            state_snapshot=state_snapshot,
        )

    @staticmethod
    def _evidence(*, source_message, source_evidence=None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if source_message is not None:
            body = str(getattr(source_message, "body", "") or "").strip()
            result.append(
                {
                    "source": "customer_message",
                    "message_id": str(getattr(source_message, "id", "")),
                    "text": body[:2000],
                }
            )
        for item in source_evidence or ():
            if not isinstance(item, Mapping):
                continue
            bounded = {
                key: item.get(key)
                for key in (
                    "category",
                    "question_type",
                    "source_id",
                    "source_type",
                    "score",
                    "metadata",
                )
                if item.get(key) is not None
            }
            if bounded and bounded not in result:
                result.append(bounded)
            if len(result) >= 10:
                break
        return result

    @staticmethod
    def _memory(source_memory=None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in source_memory or ():
            if not isinstance(item, Mapping):
                continue
            bounded = {
                key: item.get(key)
                for key in (
                    "key",
                    "confidence",
                    "source_message_id",
                    "source_type",
                )
                if item.get(key) is not None
            }
            if bounded:
                result.append(bounded)
            if len(result) >= 20:
                break
        return result

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from apps.ai_engagement.services.intent_types import Intent, IntentDecision
from apps.ai_engagement.services.organization_runtime_profile import (
    get_organization_ai_runtime_profile,
)
from apps.ai_engagement.services.retrieval import KnowledgeRetrievalService
from apps.ai_engagement.services.tenant_guard import TenantGuard


class GroundingCategory(StrEnum):
    STRUCTURED_ORG_DATA = "STRUCTURED_ORG_DATA"
    KNOWLEDGE_BASE = "KNOWLEDGE_BASE"
    CRM_DATA = "CRM_DATA"
    CONVERSATION_MEMORY = "CONVERSATION_MEMORY"
    LIVE_SYSTEM_DATA = "LIVE_SYSTEM_DATA"
    NO_VERIFIED_EVIDENCE = "NO_VERIFIED_EVIDENCE"


class InformationClass(StrEnum):
    STATIC_CONFIGURED = "STATIC_CONFIGURED"
    DYNAMIC_RETRIEVED = "DYNAMIC_RETRIEVED"
    CRM_SCOPED = "CRM_SCOPED"
    CONVERSATIONAL = "CONVERSATIONAL"
    LIVE_SYSTEM = "LIVE_SYSTEM"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EvidenceItem:
    source_id: str
    source_type: str
    content: str
    score: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "content": self.content,
            "score": self.score,
            "metadata": dict(self.metadata),
        }

    def trace_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class EvidenceResolution:
    category: GroundingCategory
    information_class: InformationClass
    question_type: str
    sensitive: bool
    verified: bool
    evidence: tuple[EvidenceItem, ...] = ()
    controlled_fallback: str = ""

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "information_class": self.information_class.value,
            "question_type": self.question_type,
            "sensitive": self.sensitive,
            "verified": self.verified,
            "evidence": [item.prompt_dict() for item in self.evidence],
            "controlled_fallback": self.controlled_fallback,
            "authority": "backend_evidence_resolver",
        }

    def trace_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "information_class": self.information_class.value,
            "question_type": self.question_type,
            "sensitive": self.sensitive,
            "verified": self.verified,
            "source_ids": [item.source_id for item in self.evidence],
            "sources": [item.trace_dict() for item in self.evidence],
            "controlled_fallback_used": bool(self.controlled_fallback and not self.verified),
            "authority": "backend_evidence_resolver",
        }


_SENSITIVE_SPECS: dict[Intent, tuple[str, tuple[str, ...]]] = {
    Intent.PRICING_QUESTION: ("pricing", ("pricing", "plans", "packages")),
    Intent.POLICY_QUESTION: ("policy", ("policies",)),
    Intent.LOCATION_QUESTION: ("location", ("locations",)),
    Intent.AVAILABILITY_QUESTION: (
        "availability",
        ("service_availability", "availability", "working_hours", "hours"),
    ),
}

_PRODUCT_SPEC = ("product_or_service", ("business_information", "services"))


class EvidenceResolver:
    """Resolve the only evidence a customer-facing answer may rely on.

    The resolver is deterministic, organization-scoped, transport-neutral and
    performs no LLM calls.  Sensitive company facts are never inferred from a
    customer message, prior assistant text, another tenant, or general model
    knowledge.
    """

    MAX_ITEMS = 4
    MAX_CONTENT_CHARS = 4000

    def resolve(
        self,
        *,
        organization,
        lead,
        question: str,
        intent_decision: IntentDecision | None = None,
        structured_memory: Mapping[str, Any] | None = None,
    ) -> EvidenceResolution:
        guard = TenantGuard(organization)
        guard.validate_lead(lead)

        question = str(question or "").strip()
        intents = self._intent_set(intent_decision)
        spec_intent = next((item for item in _SENSITIVE_SPECS if item in intents), None)

        if spec_intent is not None:
            question_type, keys = _SENSITIVE_SPECS[spec_intent]
            structured = self._structured_org_evidence(
                organization=organization,
                lead=lead,
                keys=keys,
            )
            if structured:
                return EvidenceResolution(
                    category=GroundingCategory.STRUCTURED_ORG_DATA,
                    information_class=InformationClass.STATIC_CONFIGURED,
                    question_type=question_type,
                    sensitive=True,
                    verified=True,
                    evidence=structured,
                )

            knowledge = self._knowledge_evidence(
                organization=organization,
                question=question,
                guard=guard,
            )
            if knowledge:
                return EvidenceResolution(
                    category=GroundingCategory.KNOWLEDGE_BASE,
                    information_class=InformationClass.DYNAMIC_RETRIEVED,
                    question_type=question_type,
                    sensitive=True,
                    verified=True,
                    evidence=knowledge,
                )

            return self._unknown(question_type=question_type, sensitive=True)

        lowered = question.casefold()
        if self._is_crm_status_question(lowered):
            crm = self._crm_evidence(lead)
            return EvidenceResolution(
                category=GroundingCategory.CRM_DATA,
                information_class=InformationClass.CRM_SCOPED,
                question_type="crm_status",
                sensitive=False,
                verified=bool(crm),
                evidence=crm,
                controlled_fallback=("" if crm else self._fallback("CRM status")),
            )

        if self._is_memory_question(lowered):
            memory = self._memory_evidence(structured_memory or {})
            return EvidenceResolution(
                category=(
                    GroundingCategory.CONVERSATION_MEMORY
                    if memory
                    else GroundingCategory.NO_VERIFIED_EVIDENCE
                ),
                information_class=(
                    InformationClass.CONVERSATIONAL
                    if memory
                    else InformationClass.UNKNOWN
                ),
                question_type="conversation_memory",
                sensitive=False,
                verified=bool(memory),
                evidence=memory,
                controlled_fallback=("" if memory else self._fallback("previous conversation")),
            )

        if Intent.PRODUCT_OR_SERVICE_QUESTION in intents:
            question_type, keys = _PRODUCT_SPEC
            structured = self._structured_org_evidence(
                organization=organization,
                lead=lead,
                keys=keys,
            )
            if structured:
                return EvidenceResolution(
                    category=GroundingCategory.STRUCTURED_ORG_DATA,
                    information_class=InformationClass.STATIC_CONFIGURED,
                    question_type=question_type,
                    sensitive=False,
                    verified=True,
                    evidence=structured,
                )
            knowledge = self._knowledge_evidence(
                organization=organization,
                question=question,
                guard=guard,
            )
            if knowledge:
                return EvidenceResolution(
                    category=GroundingCategory.KNOWLEDGE_BASE,
                    information_class=InformationClass.DYNAMIC_RETRIEVED,
                    question_type=question_type,
                    sensitive=False,
                    verified=True,
                    evidence=knowledge,
                )
            return self._unknown(question_type=question_type, sensitive=False)

        return EvidenceResolution(
            category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN,
            question_type="not_evidence_bound",
            sensitive=False,
            verified=False,
            evidence=(),
            controlled_fallback="",
        )

    @staticmethod
    def _intent_set(decision: IntentDecision | None) -> set[Intent]:
        if not isinstance(decision, IntentDecision):
            return set()
        return {decision.primary_intent, *decision.secondary_intents}

    def _structured_org_evidence(self, *, organization, lead, keys: tuple[str, ...]) -> tuple[EvidenceItem, ...]:
        profile = get_organization_ai_runtime_profile(
            organization=organization,
            lead=lead,
        ).as_dict()
        business_facts = profile.get("business_facts") or {}
        items: list[EvidenceItem] = []
        for key in keys:
            value = business_facts.get(key)
            if not self._meaningful(value):
                continue
            items.append(
                EvidenceItem(
                    source_id=f"organization_settings:{key}",
                    source_type="organization_runtime_profile",
                    content=self._serialize(value),
                    score=1.0,
                    metadata={"field": key},
                )
            )
            if len(items) >= self.MAX_ITEMS:
                break
        return tuple(items)

    def _knowledge_evidence(self, *, organization, question: str, guard: TenantGuard) -> tuple[EvidenceItem, ...]:
        if not question:
            return ()
        results = KnowledgeRetrievalService().retrieve_by_keyword(
            organization=organization,
            query_text=question,
            limit=self.MAX_ITEMS,
        )
        items: list[EvidenceItem] = []
        for result in results:
            chunk = getattr(result, "chunk", None)
            if chunk is None:
                continue
            guard.validate_chunk(chunk)
            content = str(getattr(chunk, "content", "") or "").strip()
            if not content:
                continue
            document_id = getattr(chunk, "document_id", None)
            score = float(getattr(result, "similarity", 0.0) or 0.0)
            items.append(
                EvidenceItem(
                    source_id=f"document:{document_id}:chunk:{getattr(chunk, 'id', '')}",
                    source_type="knowledge_chunk",
                    content=content[: self.MAX_CONTENT_CHARS],
                    score=max(0.0, min(score, 1.0)),
                    metadata={
                        "document_id": document_id,
                        "chunk_id": getattr(chunk, "id", None),
                    },
                )
            )
        return tuple(items)

    @staticmethod
    def _crm_evidence(lead) -> tuple[EvidenceItem, ...]:
        pipeline = getattr(lead, "pipeline", None)
        stage = getattr(lead, "stage", None)
        payload = {
            "pipeline_id": str(getattr(lead, "pipeline_id", "") or "") or None,
            "pipeline_name": str(getattr(pipeline, "name", "") or "") or None,
            "stage_id": str(getattr(lead, "stage_id", "") or "") or None,
            "stage_name": str(getattr(stage, "name", "") or "") or None,
        }
        if not any(payload.values()):
            return ()
        return (
            EvidenceItem(
                source_id=f"lead:{getattr(lead, 'id', '')}:crm_state",
                source_type="crm_state",
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                score=1.0,
            ),
        )

    def _memory_evidence(self, snapshot: Mapping[str, Any]) -> tuple[EvidenceItem, ...]:
        facts = snapshot.get("facts") if isinstance(snapshot, Mapping) else None
        if not isinstance(facts, Mapping):
            return ()
        items: list[EvidenceItem] = []
        for key, item in facts.items():
            if not isinstance(item, Mapping) or "value" not in item:
                continue
            items.append(
                EvidenceItem(
                    source_id=f"structured_memory:{key}",
                    source_type="structured_lead_memory",
                    content=self._serialize(item.get("value")),
                    score=max(0.0, min(float(item.get("confidence") or 0.0), 1.0)),
                    metadata={
                        "fact_key": str(key),
                        "source_message_id": item.get("source_message_id"),
                        "source_type": item.get("source_type"),
                    },
                )
            )
            if len(items) >= self.MAX_ITEMS:
                break
        return tuple(items)

    def _unknown(self, *, question_type: str, sensitive: bool) -> EvidenceResolution:
        return EvidenceResolution(
            category=GroundingCategory.NO_VERIFIED_EVIDENCE,
            information_class=InformationClass.UNKNOWN,
            question_type=question_type,
            sensitive=sensitive,
            verified=False,
            evidence=(),
            controlled_fallback=self._fallback(question_type.replace("_", " ")),
        )

    @staticmethod
    def _fallback(label: str) -> str:
        return f"I don't have verified {label} information available here, so I don't want to guess."

    @staticmethod
    def _is_crm_status_question(text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "my lead status",
                "my status",
                "which stage",
                "what stage",
                "which pipeline",
                "what pipeline",
            )
        )

    @staticmethod
    def _is_memory_question(text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "what did i tell",
                "what have i told",
                "do you remember",
                "you remember",
                "what did i say",
            )
        )

    @staticmethod
    def _meaningful(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Mapping):
            return bool(value)
        if isinstance(value, (list, tuple, set)):
            return bool(value)
        return True

    @staticmethod
    def _serialize(value: Any) -> str:
        if isinstance(value, str):
            return value[: EvidenceResolver.MAX_CONTENT_CHARS]
        try:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rendered = str(value)
        return rendered[: EvidenceResolver.MAX_CONTENT_CHARS]


__all__ = [
    "EvidenceItem",
    "EvidenceResolution",
    "EvidenceResolver",
    "GroundingCategory",
    "InformationClass",
]

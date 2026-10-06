from __future__ import annotations

import json
import re
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

_PRODUCT_SPEC = ("product_or_service", ("about", "services", "products", "business_information"))


class EvidenceResolver:
    """Resolve the only evidence a customer-facing answer may rely on.

    The resolver is deterministic, organization-scoped, transport-neutral and
    performs no LLM calls. Sensitive company facts are never inferred from a
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
                question=question,
                question_type=question_type,
            )
            knowledge = self._knowledge_evidence(
                organization=organization,
                question=question,
                guard=guard,
            )
            if structured:
                return EvidenceResolution(
                    category=GroundingCategory.STRUCTURED_ORG_DATA,
                    information_class=InformationClass.STATIC_CONFIGURED,
                    question_type=question_type,
                    sensitive=True,
                    verified=True,
                    # Category settings can be general (for example a privacy
                    # policy) while an FAQ/file answers the actual refund ask.
                    # Preserve configured facts first without hiding that answer.
                    evidence=self._bounded_evidence(structured + knowledge),
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
            return EvidenceResolution(
                category=GroundingCategory.NO_VERIFIED_EVIDENCE,
                information_class=InformationClass.CRM_SCOPED,
                question_type="internal_crm_status",
                sensitive=True,
                verified=False,
                evidence=(),
                controlled_fallback=(
                    "I can’t share internal CRM pipeline or stage details. "
                    "I can still help with your enquiry or ask the team to assist."
                ),
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
                question=question,
                question_type=question_type,
            )
            knowledge = self._knowledge_evidence(organization=organization, question=question, guard=guard)
            if structured or knowledge:
                return EvidenceResolution(
                    category=(GroundingCategory.STRUCTURED_ORG_DATA if structured
                              else GroundingCategory.KNOWLEDGE_BASE),
                    information_class=(InformationClass.STATIC_CONFIGURED if structured
                                       else InformationClass.DYNAMIC_RETRIEVED),
                    question_type=question_type,
                    sensitive=False,
                    verified=True,
                    evidence=self._bounded_evidence(knowledge + structured),
                )
            return self._unknown(question_type=question_type, sensitive=False)

        from apps.ai_engagement.services.sales_intelligence import ObjectionEngine
        objections = ObjectionEngine().detect(text=question, settings=organization.settings,
                                              intent_decision=intent_decision)
        approved = tuple(
            EvidenceItem(source_id=f"organization:{organization.pk}:objection:{item.category}:{index}",
                         source_type="organization_approved_objection_fact", content=fact,
                         metadata={"category": item.category})
            for item in objections for index, fact in enumerate(item.approved_facts)
        )[:self.MAX_ITEMS]
        if approved:
            return EvidenceResolution(
                category=GroundingCategory.STRUCTURED_ORG_DATA,
                information_class=InformationClass.STATIC_CONFIGURED,
                question_type="objection", sensitive=False, verified=True, evidence=approved)

        # Follow-up/ambiguous intents may still carry an explicit knowledge
        # requirement. Do not force these questions to depend on FAQ wording.
        if isinstance(intent_decision, IntentDecision) and intent_decision.requires_knowledge:
            knowledge = self._knowledge_evidence(organization=organization, question=question, guard=guard)
            if knowledge:
                return EvidenceResolution(
                    category=GroundingCategory.KNOWLEDGE_BASE,
                    information_class=InformationClass.DYNAMIC_RETRIEVED,
                    question_type="product_or_service", sensitive=False,
                    verified=True, evidence=knowledge,
                )
            return self._unknown(question_type="product_or_service", sensitive=False)

        from apps.ai_engagement.services.authored_knowledge import matching_authored_answers
        answers = matching_authored_answers(organization=organization, question=question, limit=self.MAX_ITEMS)
        if answers:
            return EvidenceResolution(
                category=GroundingCategory.STRUCTURED_ORG_DATA,
                information_class=InformationClass.STATIC_CONFIGURED,
                question_type="product_or_service", sensitive=False, verified=True,
                evidence=tuple(EvidenceItem(source_id=item["source_id"], source_type=item["source_type"],
                                           content=item["content"][:self.MAX_CONTENT_CHARS]) for item in answers),
            )
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

    def _structured_org_evidence(
        self, *, organization, lead, keys: tuple[str, ...], question: str = "", question_type: str = "",
    ) -> tuple[EvidenceItem, ...]:
        profile = get_organization_ai_runtime_profile(
            organization=organization,
            lead=lead,
        ).as_dict()
        business_facts = profile.get("business_facts") or {}
        business_information = profile.get("business_information") or {}
        configured_information = (
            business_information.get("configured")
            if isinstance(business_information, Mapping)
            else {}
        ) or {}
        items: list[EvidenceItem] = []
        for key in keys:
            value = business_facts.get(key) if isinstance(business_facts, Mapping) else None
            source_area = "business_facts"
            if not self._meaningful(value):
                if key == "about" and isinstance(business_information, Mapping):
                    value = business_information.get("about")
                    source_area = "business_information"
                elif isinstance(configured_information, Mapping):
                    value = configured_information.get(key)
                    source_area = "business_information.configured"
            if not self._meaningful(value):
                continue
            content = (self._about_excerpt(value, question=question, question_type=question_type)
                       if key == "about" and question_type == "product_or_service"
                       else self._serialize(value))
            if not content:
                continue
            items.append(
                EvidenceItem(
                    source_id=f"organization_profile:{source_area}:{key}",
                    source_type="organization_runtime_profile",
                    content=content,
                    score=1.0,
                    metadata={"field": key, "area": source_area},
                )
            )
            if len(items) >= self.MAX_ITEMS:
                break
        # About is authored company knowledge, including prices and policies.
        # Keep private Playbook instructions out of customer-fact evidence.
        if question_type in {"pricing", "policy", "location", "availability"}:
            about = business_information.get("about") if isinstance(business_information, Mapping) else ""
            excerpt = self._about_excerpt(about, question=question, question_type=question_type)
            if excerpt:
                items = items[:self.MAX_ITEMS - 1]
                items.append(EvidenceItem(
                    source_id="organization_profile:business_information:about",
                    source_type="organization_runtime_profile",
                    content=excerpt,
                    metadata={"field": "about", "area": "business_information"},
                ))
        return tuple(items[:self.MAX_ITEMS])

    @classmethod
    def _about_excerpt(cls, about, *, question: str, question_type: str) -> str:
        """Select topical facts for the independent verifier, never instructions."""
        from apps.ai_engagement.services.retrieval import _query_tokens

        patterns = {
            "pricing": r"\b(?:pric\w*|costs?|fees?|plans?|packages?|discounts?|free|inr|usd|rupees?|dollars?)\b|[₹$€£]\s*\d",
            "policy": r"\b(?:polic\w*|refund\w*|cancell?\w*|warrant\w*|returns?|privacy|terms)\b",
            "location": r"\b(?:locat\w*|address|offices?|branches?|based|headquarters)\b",
            "product_or_service": r"\S",
            "availability": r"\b(?:availab\w*|offer\w*|hours?|timings?|opening|closing|open|closed|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        }
        pattern = patterns.get(question_type)
        if not pattern:
            return ""
        query = set(_query_tokens(question))
        # A price paragraph without its parent heading can lose eligibility,
        # billing period or refund exceptions. Keep complete scoped source units.
        source = str(about or "").strip()
        segments = re.split(r"(?=^#{1,6}\s+\S)", source, flags=re.M)
        parts = []
        parents = []
        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue
            heading = re.match(r"^(#{1,6})\s+", segment)
            if not heading:
                parents = [(0, segment)]
                parts.append(segment)
                continue
            depth = len(heading.group(1))
            parents = [(level, content) for level, content in parents if level < depth]
            scoped = "\n\n".join([content for _, content in parents] + [segment])
            parts.append(scoped)
            parents.append((depth, segment))
        ranked = []
        for index, part in enumerate(parts):
            part = part.strip()
            if not part or not re.search(pattern, part, re.I):
                continue
            overlap = len(query & set(_query_tokens(part)))
            if question_type == "product_or_service" and not overlap:
                continue
            ranked.append((overlap, index, part))
        remaining = cls.MAX_CONTENT_CHARS
        selected = []
        for _, _, part in sorted(ranked, key=lambda row: (-row[0], row[1])):
            if remaining <= 0:
                break
            if len(part) > remaining:
                continue
            selected.append(part)
            remaining -= len(part) + 2
        return "\n\n".join(selected)[:cls.MAX_CONTENT_CHARS]

    def _knowledge_evidence(self, *, organization, question: str, guard: TenantGuard) -> tuple[EvidenceItem, ...]:
        if not question:
            return ()
        from apps.ai_engagement.services.authored_knowledge import matching_authored_answers
        authored = matching_authored_answers(organization=organization, question=question, limit=self.MAX_ITEMS)
        authored_items = tuple(EvidenceItem(
            source_id=item["source_id"], source_type=item["source_type"],
            content=item["content"][:self.MAX_CONTENT_CHARS], score=item["score"],
            metadata={"question": item["question"]},
        ) for item in authored)
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
        matched = (authored_items + tuple(items))[:self.MAX_ITEMS]
        if matched:
            return matched

        # The reply model already understands multilingual and paraphrased
        # questions. Give its existing independent verifier the complete authored
        # Q/A instead of declaring knowledge absent solely from lexical mismatch.
        from apps.ai_engagement.services.authored_knowledge import authored_answer_candidates
        return tuple(EvidenceItem(
            source_id=item['source_id'], source_type=item['source_type'],
            content=item['content'], score=0.0,
            metadata={'requires_relevance_verification': True,
                      'retrieval_path': 'authored_faq_candidates'},
        ) for item in authored_answer_candidates(organization=organization, question=question))

    @staticmethod
    def _bounded_evidence(items) -> tuple[EvidenceItem, ...]:
        """Bound merged evidence while retaining complete conditional Q/A pairs."""
        selected, remaining = [], 12000
        for item in items:
            if len(item.content) > remaining:
                continue
            selected.append(item)
            remaining -= len(item.content)
            if len(selected) >= 12:
                break
        return tuple(selected)

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
        # Product questions about configurable pipelines/stages are public
        # knowledge requests. Only requests for a person's recorded CRM state
        # belong to the private-state boundary.
        return bool(re.search(
            r"\bmy\s+(?:(?:current|crm|lead|sales)\s+){0,3}(?:stage|pipeline|lead status)\b|"
            r"\b(?:stage|pipeline)\b.{0,70}\b(?:am i in|was i in|have i been|"
            r"you (?:put|placed|assigned|moved) me|for (?:me|this lead|that lead))\b|"
            r"\b(?:this|that) lead(?:'s)?\b.{0,40}\b(?:stage|pipeline|status)\b",
            text, flags=re.IGNORECASE,
        ))

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

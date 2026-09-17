from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.models.lead_memory import LeadMemory
from apps.ai_engagement.models.org_info import OrgInfo
from apps.ai_engagement.services.organization_profile import (
    compile_qualification_requirements,
)
from apps.ai_engagement.services.qualification_state import (
    REQUIREMENT_ANSWERED,
    requirements_for_lead,
    state_for_lead,
)
from apps.channels.models import WhatsAppMessage
from apps.crm.models import AttributeDefinition, Lead


class LeadMemoryError(Exception):
    """Base error for structured lead-memory operations."""


class LeadMemoryScopeError(LeadMemoryError):
    """Raised when an organization/lead/message scope does not match."""


MAX_FACTS = 200
MAX_EVENTS = 100
MAX_EVIDENCE_HISTORY = 5
MAX_EVIDENCE_CHARS = 500
MAX_EVENT_CHARS = 240

_SOURCE_PRIORITY = {
    "explicit_message": 1,
    "qualification": 2,
    "crm_attribute": 3,
}

_CONCEPT_ALIASES = {
    "budget": ("budget", "price range", "spend", "amount"),
    "timeline": ("timeline", "timeframe", "time frame", "start date", "go live"),
    "product_interest": ("product", "service", "interest", "requirement"),
    "location": ("location", "city", "state", "country", "based in"),
    "lead_volume": ("lead volume", "leads per", "daily leads", "monthly leads", "lead count"),
    "current_tools": ("current tool", "tools", "software", "crm", "platform"),
    "decision_maker_status": ("decision maker", "decision-maker", "authority", "approver"),
    "preferred_contact_time": ("contact time", "call time", "callback time", "preferred time"),
    "language": ("language", "speak in", "chat in"),
    "preference": ("preference", "preferred", "prefer"),
}

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "which",
    "your",
}

_BUDGET_RE = re.compile(
    r"\b(?:my|our|the)?\s*budget\s*(?:is|=|:|around|about|of)?\s*"
    r"(?P<value>(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)?\s*\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:k|lakh|lakhs|crore|crores|thousand|million))?"
    r"(?:\s*(?:-|–|to)\s*(?:₹|rs\.?|inr|\$|usd|€|eur|£|gbp)?\s*\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:k|lakh|lakhs|crore|crores|thousand|million))?)?)",
    re.IGNORECASE,
)
_LEAD_VOLUME_RE = re.compile(
    r"\b(?P<value>\d[\d,]*\s+(?:new\s+)?leads?\s+(?:per|a|every)\s+(?:day|week|month))\b",
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"\b(?:i(?:'m| am)?|we(?:'re| are)?)?\s*(?:based|located)\s+in\s+"
    r"(?P<value>[A-Za-z][A-Za-z0-9 ,.'-]{1,80})(?=$|[.!?])",
    re.IGNORECASE,
)
_TOOL_RE = re.compile(
    r"\b(?:i|we)\s+(?:currently\s+)?(?:use|am using|are using)\s+"
    r"(?P<value>[^.!?\n]{1,100})(?=$|[.!?])",
    re.IGNORECASE,
)
_INTEREST_RE = re.compile(
    r"\b(?:i(?:'m| am)?|we(?:'re| are)?)\s+(?:interested in|looking for)\s+"
    r"(?P<value>[^.!?\n]{1,120})(?=$|[.!?])",
    re.IGNORECASE,
)
_TIMELINE_RE = re.compile(
    r"\b(?:timeline|timeframe|time frame)\s*(?:is|=|:)?\s+"
    r"(?P<value>[^.!?\n]{1,80})(?=$|[.!?])",
    re.IGNORECASE,
)
_START_TIMELINE_RE = re.compile(
    r"\b(?:i|we)\s+(?:need|want|plan)\s+to\s+(?:start|launch|buy|implement|go live)\s+"
    r"(?P<value>(?:by|in|within|around)\s+[^.!?\n]{1,70})(?=$|[.!?])",
    re.IGNORECASE,
)
_CONTACT_TIME_RE = re.compile(
    r"\b(?:call|contact|reach)\s+me\s+"
    r"(?P<value>(?:at|after|before|between)\s+[^.!?\n]{1,70})(?=$|[.!?])",
    re.IGNORECASE,
)
_LANGUAGE_RE = re.compile(
    r"\b(?:my\s+preferred\s+language\s+is|please\s+(?:speak|chat)\s+in|"
    r"i\s+prefer\s+to\s+(?:speak|chat)\s+in)\s+"
    r"(?P<value>[A-Za-z][A-Za-z -]{1,35})(?=$|[.!?])",
    re.IGNORECASE,
)
_PREFERENCE_RE = re.compile(
    r"\b(?:i|we)\s+prefer\s+(?P<value>[^.!?\n]{1,100})(?=$|[.!?])",
    re.IGNORECASE,
)
_DECISION_MAKER_RE = re.compile(
    r"\b(?:i\s+am|i'm)\s+(?:the\s+)?decision[- ]maker\b",
    re.IGNORECASE,
)
_NOT_SOLE_DECISION_MAKER_RE = re.compile(
    r"\b(?:i|we)\s+(?:need|have)\s+to\s+(?:ask|check|confirm|discuss)\s+(?:it\s+)?with\s+"
    r"(?:my|our|the)\s+(?:boss|manager|partner|team|director|owner|founder)\b",
    re.IGNORECASE,
)
_CORRECTION_RE = re.compile(
    r"\b(?:actually|correction|correct that|change my|change it|instead|i meant|update my|replace my)\b",
    re.IGNORECASE,
)

_OBJECTION_TERMS = (
    "too expensive",
    "price is high",
    "cost is high",
    "not interested",
    "need to think",
    "have to think",
    "not convinced",
    "too costly",
    "cannot afford",
    "can't afford",
)


class LeadMemoryService:
    """Python-owned organization-scoped lead memory.

    The service performs no AI/provider calls. It only consumes state that the
    backend already owns: organization CRM fields, validated qualification state,
    and explicit inbound-message evidence.
    """

    @transaction.atomic
    def update_from_accepted_turn(
        self,
        *,
        organization,
        lead: Lead,
        source_message_id,
    ) -> dict[str, Any]:
        self._validate_lead_scope(organization=organization, lead=lead)

        scoped_lead = (
            Lead.objects.select_for_update()
            .select_related("organization", "pipeline", "stage")
            .get(pk=lead.pk, organization_id=organization.id)
        )
        source = (
            WhatsAppMessage.objects.filter(
                pk=source_message_id,
                organization_id=organization.id,
                lead_id=scoped_lead.id,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .first()
        )
        if source is None:
            raise LeadMemoryScopeError(
                "Memory source message does not belong to this organization and lead."
            )

        memory = (
            LeadMemory.objects.select_for_update()
            .filter(organization_id=organization.id, lead_id=scoped_lead.id)
            .first()
        )
        if memory is None:
            memory = LeadMemory.objects.create(
                organization_id=organization.id,
                lead_id=scoped_lead.id,
            )

        definitions = list(
            AttributeDefinition.objects.filter(organization_id=organization.id)
            .order_by("display_order", "created_at")
        )
        facts = deepcopy(memory.structured_facts) if isinstance(memory.structured_facts, dict) else {}
        events = deepcopy(memory.long_term_events) if isinstance(memory.long_term_events, list) else []
        report: dict[str, Any] = {
            "identity": {
                "organization_id": str(organization.id),
                "lead_id": str(scoped_lead.id),
            },
            "source_message_id": str(source.id),
            "applied": [],
            "rejected": [],
            "events_added": [],
        }

        candidates: list[dict[str, Any]] = []
        candidates.extend(
            self._crm_attribute_candidates(
                lead=scoped_lead,
                definitions=definitions,
            )
        )
        candidates.extend(
            self._qualification_candidates(
                lead=scoped_lead,
                definitions=definitions,
            )
        )
        candidates.extend(
            self._explicit_message_candidates(
                text=str(source.body or ""),
                message=source,
                definitions=definitions,
            )
        )

        explicit_correction = bool(_CORRECTION_RE.search(str(source.body or "")))
        for candidate in candidates:
            self._merge_candidate(
                facts=facts,
                candidate=candidate,
                report=report,
                explicit_correction=explicit_correction,
            )

        for event in self._explicit_long_term_events(message=source):
            if self._append_event(events=events, event=event):
                report["events_added"].append(
                    {
                        "type": event["type"],
                        "event_id": event["id"],
                        "source_message_id": event["source_message_id"],
                    }
                )

        memory.structured_facts = self._trim_facts(facts)
        memory.long_term_events = events[-MAX_EVENTS:]
        memory.source_last_message_id = source.id
        memory.source_last_message_at = source.created_at
        memory.save(
            update_fields=[
                "structured_facts",
                "long_term_events",
                "source_last_message_id",
                "source_last_message_at",
                "updated_at",
            ]
        )

        trace_payload = self._trace_payload(report=report, memory=memory)
        transaction.on_commit(
            lambda payload=trace_payload: self._record_trace(payload)
        )
        return report

    def get_provider_snapshot(
        self,
        *,
        organization_id,
        lead_id,
    ) -> dict[str, Any]:
        """Return memory for an exact tenant+lead identity.

        Current organization-defined CRM values are overlaid as authoritative
        runtime facts so a recently edited CRM field never waits for a later
        memory refresh before reaching the engagement context.
        """
        lead = (
            Lead.objects.filter(pk=lead_id, organization_id=organization_id)
            .only("id", "organization_id", "attributes")
            .first()
        )
        if lead is None:
            raise LeadMemoryScopeError("Lead memory scope is invalid.")

        memory = (
            LeadMemory.objects.filter(
                organization_id=organization_id,
                lead_id=lead_id,
            )
            .only("structured_facts", "long_term_events", "source_last_message_id", "source_last_message_at")
            .first()
        )
        facts = (
            deepcopy(memory.structured_facts)
            if memory is not None and isinstance(memory.structured_facts, dict)
            else {}
        )
        events = (
            deepcopy(memory.long_term_events)
            if memory is not None and isinstance(memory.long_term_events, list)
            else []
        )
        definitions = list(
            AttributeDefinition.objects.filter(organization_id=organization_id)
            .order_by("display_order", "created_at")
        )
        runtime_report = {"applied": [], "rejected": []}
        for candidate in self._crm_attribute_candidates(
            lead=lead,
            definitions=definitions,
        ):
            self._merge_candidate(
                facts=facts,
                candidate=candidate,
                report=runtime_report,
                explicit_correction=False,
            )

        return {
            "identity": {
                "organization_id": str(organization_id),
                "lead_id": str(lead_id),
            },
            "structured_facts": self._trim_facts(facts),
            "long_term_events": events[-MAX_EVENTS:],
            "source_last_message_id": (
                str(memory.source_last_message_id)
                if memory is not None and memory.source_last_message_id
                else None
            ),
            "source_last_message_at": (
                memory.source_last_message_at.isoformat()
                if memory is not None and memory.source_last_message_at
                else None
            ),
        }

    def augment_provider_payload(
        self,
        *,
        payload: dict[str, Any],
        organization_id,
        lead_id,
    ) -> dict[str, Any]:
        snapshot = self.get_provider_snapshot(
            organization_id=organization_id,
            lead_id=lead_id,
        )
        result = deepcopy(payload)
        result["structured_lead_memory"] = snapshot["structured_facts"]
        result["long_term_memory"] = {
            "conversation_summary": result.get("conversation_summary"),
            "important_events": snapshot["long_term_events"],
        }
        return result

    def _validate_lead_scope(self, *, organization, lead: Lead) -> None:
        if organization is None or lead is None:
            raise LeadMemoryScopeError("Organization and lead are required.")
        if str(getattr(lead, "organization_id", "")) != str(getattr(organization, "id", "")):
            raise LeadMemoryScopeError("Lead does not belong to this organization.")

    def _crm_attribute_candidates(
        self,
        *,
        lead: Lead,
        definitions: list[AttributeDefinition],
    ) -> list[dict[str, Any]]:
        attributes = lead.attributes if isinstance(lead.attributes, dict) else {}
        candidates: list[dict[str, Any]] = []
        for definition in definitions:
            if definition.key not in attributes:
                continue
            value = attributes.get(definition.key)
            if self._is_empty(value):
                continue
            candidates.append(
                self._candidate(
                    key=f"attribute:{definition.key}",
                    concept=self._concept_for_text(
                        f"{definition.key} {definition.name} {definition.description}"
                    ) or "organization_field",
                    value=value,
                    confidence=1.0,
                    source="crm_attribute",
                    field=definition,
                    evidence={
                        "type": "crm_attribute",
                        "message_id": None,
                        "reference": f"lead.attributes.{definition.key}",
                        "text": "",
                    },
                )
            )
        return candidates

    def _qualification_candidates(
        self,
        *,
        lead: Lead,
        definitions: list[AttributeDefinition],
    ) -> list[dict[str, Any]]:
        org_info = OrgInfo.objects.filter(organization_id=lead.organization_id).first()
        current_requirements = compile_qualification_requirements(
            org_info.qualification_requirements if org_info else ""
        )["requirements"]
        requirements = requirements_for_lead(lead, current_requirements)
        state = state_for_lead(lead, requirements=requirements)
        states = state.get("requirement_states") or {}
        candidates: list[dict[str, Any]] = []

        for requirement in requirements:
            requirement_id = str(requirement.get("id") or "").strip()
            requirement_state = states.get(requirement_id) or {}
            if requirement_state.get("status") != REQUIREMENT_ANSWERED:
                continue
            value = requirement_state.get("value")
            if self._is_empty(value):
                continue

            authored_text = " ".join(
                str(part or "")
                for part in (
                    requirement.get("label"),
                    requirement.get("question"),
                    requirement.get("stable_id"),
                )
            )
            concept = self._concept_for_text(authored_text) or "organization_requirement"
            definition = self._match_definition(
                definitions=definitions,
                concept=concept,
                authored_text=authored_text,
            )
            stable_id = str(
                requirement.get("stable_id") or requirement_id or "requirement"
            )
            source_id = str(requirement_state.get("source_message_id") or "") or None
            raw_answer = self._clip(requirement_state.get("raw_answer"), MAX_EVIDENCE_CHARS)
            confidence_label = str(requirement_state.get("confidence") or "").casefold()
            confidence = 0.97 if confidence_label == "high" else 0.95
            candidates.append(
                self._candidate(
                    key=(
                        f"attribute:{definition.key}"
                        if definition is not None
                        else f"qualification:{stable_id}"
                    ),
                    concept=concept,
                    value=value,
                    confidence=confidence,
                    source="qualification",
                    field=definition,
                    evidence={
                        "type": "validated_qualification_answer",
                        "message_id": source_id,
                        "reference": stable_id,
                        "text": raw_answer,
                    },
                )
            )
        return candidates

    def _explicit_message_candidates(
        self,
        *,
        text: str,
        message: WhatsAppMessage,
        definitions: list[AttributeDefinition],
    ) -> list[dict[str, Any]]:
        text = str(text or "").strip()
        if not text:
            return []

        extracted: list[tuple[str, Any, str]] = []
        for concept, regex in (
            ("budget", _BUDGET_RE),
            ("lead_volume", _LEAD_VOLUME_RE),
            ("location", _LOCATION_RE),
            ("current_tools", _TOOL_RE),
            ("product_interest", _INTEREST_RE),
            ("timeline", _TIMELINE_RE),
            ("timeline", _START_TIMELINE_RE),
            ("preferred_contact_time", _CONTACT_TIME_RE),
            ("language", _LANGUAGE_RE),
            ("preference", _PREFERENCE_RE),
        ):
            match = regex.search(text)
            if match is None:
                continue
            value = self._clip(match.group("value"), 160).strip(" ,;:-")
            if not value:
                continue
            extracted.append((concept, value, self._clip(match.group(0), MAX_EVIDENCE_CHARS)))

        if _DECISION_MAKER_RE.search(text):
            match = _DECISION_MAKER_RE.search(text)
            extracted.append(
                (
                    "decision_maker_status",
                    "decision_maker",
                    self._clip(match.group(0), MAX_EVIDENCE_CHARS),
                )
            )
        elif _NOT_SOLE_DECISION_MAKER_RE.search(text):
            match = _NOT_SOLE_DECISION_MAKER_RE.search(text)
            extracted.append(
                (
                    "decision_maker_status",
                    "requires_other_approval",
                    self._clip(match.group(0), MAX_EVIDENCE_CHARS),
                )
            )

        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for concept, value, evidence_text in extracted:
            marker = (concept, json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))
            if marker in seen:
                continue
            seen.add(marker)
            definition = self._match_definition(
                definitions=definitions,
                concept=concept,
                authored_text=concept.replace("_", " "),
            )
            result.append(
                self._candidate(
                    key=(
                        f"attribute:{definition.key}"
                        if definition is not None
                        else f"concept:{concept}"
                    ),
                    concept=concept,
                    value=value,
                    confidence=0.85,
                    source="explicit_message",
                    field=definition,
                    evidence={
                        "type": "explicit_inbound_text",
                        "message_id": str(message.id),
                        "reference": None,
                        "text": evidence_text,
                    },
                )
            )
        return result

    def _explicit_long_term_events(self, *, message: WhatsAppMessage) -> list[dict[str, Any]]:
        text = str(message.body or "").strip()
        if not text:
            return []
        sentences = [
            " ".join(part.split())
            for part in re.split(r"[.!?\n]+", text)
            if part.strip()
        ]
        events: list[dict[str, Any]] = []
        for sentence in sentences:
            lowered = sentence.casefold()
            event_types: list[str] = []
            if any(term in lowered for term in _OBJECTION_TERMS):
                event_types.append("objection")
            if (
                any(term in lowered for term in ("call", "demo", "meeting", "appointment"))
                and any(
                    term in lowered
                    for term in (
                        "booked",
                        "scheduled",
                        "schedule",
                        "today",
                        "tomorrow",
                        " at ",
                        " on ",
                    )
                )
            ):
                event_types.append("appointment")
            if re.search(r"\b(?:i|we)\s+(?:will|'ll)\b", lowered):
                event_types.append("commitment")
            if re.search(r"\b(?:i|we)\s+(?:decided|chose|choose|will go with|want to go with)\b", lowered):
                event_types.append("decision")
            if (
                any(term in lowered for term in ("human", "agent", "representative", "sales person", "salesperson"))
                and any(term in lowered for term in ("speak", "talk", "connect", "transfer"))
            ):
                event_types.append("handoff")

            for event_type in event_types:
                summary = self._clip(sentence, MAX_EVENT_CHARS)
                event_id = hashlib.sha256(
                    f"{message.id}:{event_type}:{summary.casefold()}".encode("utf-8")
                ).hexdigest()[:24]
                events.append(
                    {
                        "id": event_id,
                        "type": event_type,
                        "summary": summary,
                        "confidence": 0.9,
                        "source": "explicit_message",
                        "source_message_id": str(message.id),
                        "evidence": self._clip(sentence, MAX_EVIDENCE_CHARS),
                        "occurred_at": (
                            message.created_at.isoformat()
                            if message.created_at
                            else None
                        ),
                        "recorded_at": timezone.now().isoformat(),
                    }
                )
        return events

    def _candidate(
        self,
        *,
        key: str,
        concept: str,
        value,
        confidence: float,
        source: str,
        field: AttributeDefinition | None,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "key": key,
            "concept": concept,
            "value": self._json_value(value),
            "confidence": float(confidence),
            "source": source,
            "field": (
                {
                    "key": field.key,
                    "name": field.name,
                    "field_type": field.field_type,
                }
                if field is not None
                else None
            ),
            "evidence": {
                "type": str(evidence.get("type") or ""),
                "message_id": evidence.get("message_id"),
                "reference": evidence.get("reference"),
                "text": self._clip(evidence.get("text"), MAX_EVIDENCE_CHARS),
            },
            "updated_at": timezone.now().isoformat(),
        }

    def _merge_candidate(
        self,
        *,
        facts: dict[str, Any],
        candidate: dict[str, Any],
        report: dict[str, Any],
        explicit_correction: bool,
    ) -> None:
        key = str(candidate["key"])
        existing = facts.get(key)
        if not isinstance(existing, dict):
            facts[key] = candidate
            report.setdefault("applied", []).append(self._report_item(candidate, action="created"))
            return

        if self._values_equal(existing.get("value"), candidate.get("value")):
            merged = deepcopy(existing)
            old_confidence = self._confidence(existing)
            new_confidence = self._confidence(candidate)
            if (
                new_confidence > old_confidence
                or self._source_priority(candidate) > self._source_priority(existing)
            ):
                for field_name in ("confidence", "source", "field", "evidence", "updated_at", "concept"):
                    merged[field_name] = deepcopy(candidate.get(field_name))
            history = list(merged.get("evidence_history") or [])
            candidate_evidence = deepcopy(candidate.get("evidence") or {})
            if candidate_evidence and candidate_evidence not in history:
                history.append(candidate_evidence)
            merged["evidence_history"] = history[-MAX_EVIDENCE_HISTORY:]
            merged["last_confirmed_at"] = candidate["updated_at"]
            facts[key] = merged
            report.setdefault("applied", []).append(self._report_item(candidate, action="confirmed"))
            return

        old_confidence = self._confidence(existing)
        new_confidence = self._confidence(candidate)
        old_priority = self._source_priority(existing)
        new_priority = self._source_priority(candidate)
        authoritative_refresh = candidate.get("source") == "crm_attribute"
        validated_refresh = candidate.get("source") == "qualification" and new_priority >= old_priority
        safe_correction = explicit_correction and new_priority >= old_priority

        if not (
            authoritative_refresh
            or validated_refresh
            or new_confidence > old_confidence
            or (new_confidence == old_confidence and new_priority > old_priority)
            or safe_correction
        ):
            report.setdefault("rejected", []).append(
                {
                    **self._report_item(candidate, action="rejected"),
                    "reason": "lower_confidence_or_authority_than_existing_fact",
                    "existing_confidence": old_confidence,
                    "existing_source": str(existing.get("source") or ""),
                }
            )
            return

        previous = {
            "value": self._json_value(existing.get("value")),
            "confidence": old_confidence,
            "source": existing.get("source"),
            "evidence": deepcopy(existing.get("evidence") or {}),
            "updated_at": existing.get("updated_at"),
        }
        replacement = deepcopy(candidate)
        history = list(existing.get("history") or [])
        history.append(previous)
        replacement["history"] = history[-MAX_EVIDENCE_HISTORY:]
        facts[key] = replacement
        report.setdefault("applied", []).append(self._report_item(candidate, action="updated"))

    def _match_definition(
        self,
        *,
        definitions: list[AttributeDefinition],
        concept: str,
        authored_text: str,
    ) -> AttributeDefinition | None:
        authored_tokens = self._tokens(authored_text)
        aliases = _CONCEPT_ALIASES.get(concept, ())
        for definition in definitions:
            field_text = f"{definition.key} {definition.name} {definition.description}"
            field_tokens = self._tokens(field_text)
            if field_tokens and authored_tokens and (
                field_tokens.issubset(authored_tokens)
                or authored_tokens.issubset(field_tokens)
            ):
                return definition
        for definition in definitions:
            normalized = self._normalize(
                f"{definition.key} {definition.name} {definition.description}"
            )
            if any(self._normalize(alias) in normalized for alias in aliases):
                return definition
        return None

    def _concept_for_text(self, value: str) -> str:
        normalized = self._normalize(value)
        for concept, aliases in _CONCEPT_ALIASES.items():
            if any(self._normalize(alias) in normalized for alias in aliases):
                return concept
        return ""

    def _append_event(self, *, events: list[dict[str, Any]], event: dict[str, Any]) -> bool:
        event_id = str(event.get("id") or "")
        if not event_id:
            return False
        if any(str(item.get("id") or "") == event_id for item in events if isinstance(item, dict)):
            return False
        events.append(event)
        del events[:-MAX_EVENTS]
        return True

    def _trim_facts(self, facts: dict[str, Any]) -> dict[str, Any]:
        if len(facts) <= MAX_FACTS:
            return facts
        ranked = sorted(
            facts.items(),
            key=lambda item: (
                self._confidence(item[1] if isinstance(item[1], dict) else {}),
                self._source_priority(item[1] if isinstance(item[1], dict) else {}),
                str((item[1] if isinstance(item[1], dict) else {}).get("updated_at") or ""),
            ),
            reverse=True,
        )[:MAX_FACTS]
        return dict(ranked)

    def _trace_payload(self, *, report: dict[str, Any], memory: LeadMemory) -> dict[str, Any]:
        return {
            "status": "updated",
            "identity": report["identity"],
            "source_message_id": report["source_message_id"],
            "updates": report["applied"],
            "rejected_updates": report["rejected"],
            "events_added": report["events_added"],
            "structured_fact_count": len(memory.structured_facts or {}),
            "long_term_event_count": len(memory.long_term_events or []),
        }

    def _record_trace(self, payload: dict[str, Any]) -> None:
        try:
            from apps.ai_engagement.services.trace_service import record

            record("memory", payload)
        except Exception:
            # Observability must never become memory or delivery authority.
            return

    def _report_item(self, candidate: dict[str, Any], *, action: str) -> dict[str, Any]:
        evidence = candidate.get("evidence") or {}
        return {
            "key": candidate.get("key"),
            "concept": candidate.get("concept"),
            "action": action,
            "source": candidate.get("source"),
            "confidence": candidate.get("confidence"),
            "evidence_message_id": evidence.get("message_id"),
            "field_key": (candidate.get("field") or {}).get("key"),
        }

    def _source_priority(self, fact: dict[str, Any]) -> int:
        return _SOURCE_PRIORITY.get(str(fact.get("source") or ""), 0)

    def _confidence(self, fact: dict[str, Any]) -> float:
        try:
            return float(fact.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _tokens(self, value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", self._normalize(value))
            if token not in _STOPWORDS and len(token) > 1
        }

    def _normalize(self, value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

    def _clip(self, value, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text[:limit]

    def _is_empty(self, value) -> bool:
        return value is None or value == "" or value == [] or value == {}

    def _json_value(self, value):
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))

    def _values_equal(self, left, right) -> bool:
        return self._json_value(left) == self._json_value(right)

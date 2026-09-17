from __future__ import annotations

import re
from copy import deepcopy
from functools import wraps
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.grounding_validation import (
    is_social_only_reply,
    matches_verified_evidence,
)


_INSTALLED = False

_SOURCE_AUTHORITY = {
    "verified_crm": 100,
    "validated_qualification": 95,
    "system_event": 90,
    "explicit_customer": 85,
    "phase2_intent": 70,
    "intent_fact": 70,
    "conversation_extraction": 50,
    "model_inference": 30,
}

_WORKING_HOURS_RE = re.compile(
    r"\b(?:working\s+hours?|business\s+hours?|office\s+hours?|opening\s+hours?|"
    r"closing\s+hours?|timings?|hours?|when\s+do\s+you\s+(?:open|close)|"
    r"what\s+time\s+do\s+you\s+(?:open|close))\b",
    re.I,
)
_LIVE_SLOT_RE = re.compile(
    r"\b(?:slots?|appointments?|bookings?|book\s+(?:a|an|the)|free\s+at|available\s+at)\b"
    r"|अपॉइंटमेंट|अपॉइंटमेंट|स्लॉट|बुकिंग",
    re.I,
)


def _authority(source_type: Any) -> int:
    return _SOURCE_AUTHORITY.get(str(source_type or "").strip().casefold(), 50)


def _bounded_confidence(value: Any) -> float:
    try:
        parsed = float(value if value is not None else 0.8)
    except (TypeError, ValueError):
        parsed = 0.0
    return max(0.0, min(parsed, 1.0))


def _fresh_lead(*, organization, lead):
    from apps.ai_engagement.services.structured_memory import StructuredMemoryScopeError
    from apps.crm.models import Lead

    fresh = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(pk=lead.pk, organization_id=organization.id)
        .first()
    )
    if fresh is None:
        raise StructuredMemoryScopeError(
            "Lead is outside the active organization scope."
        )
    return fresh


def _canonical_backend_facts(*, organization, lead):
    """Expose canonical CRM/qualification facts without persisting a rival copy."""
    from apps.ai_engagement.services.organization_runtime_profile import (
        get_organization_ai_runtime_profile,
    )
    from apps.ai_engagement.services.qualification_state import (
        REQUIREMENT_ANSWERED,
        requirements_for_lead,
        state_for_lead,
    )
    from apps.ai_engagement.services.tenant_guard import TenantGuard

    TenantGuard(organization).validate_current_lead_context(lead)
    fresh = _fresh_lead(organization=organization, lead=lead)
    TenantGuard(organization).validate_current_lead_context(fresh)

    profile = get_organization_ai_runtime_profile(
        organization=organization,
        lead=lead,
    )
    profile_data = profile.as_dict()
    crm = profile_data.get("crm_capabilities") or {}
    definitions = crm.get("attributes") if isinstance(crm, Mapping) else []
    definition_keys = {
        str(item.get("key") or "").strip()
        for item in definitions or []
        if isinstance(item, Mapping) and str(item.get("key") or "").strip()
    }

    lead_attributes = getattr(fresh, "attributes", None) or {}
    canonical: dict[str, dict[str, Any]] = {}
    now = timezone.now().isoformat()
    for key in definition_keys:
        if key not in lead_attributes:
            continue
        canonical[key] = {
            "value": deepcopy(lead_attributes.get(key)),
            "confidence": 1.0,
            "source_message_id": None,
            "source_type": "verified_crm",
            "evidence": "",
            "updated_at": now,
        }

    requirements = requirements_for_lead(
        fresh,
        profile.configured_requirements(),
    )
    state = state_for_lead(fresh, requirements=requirements)
    mappings: dict[str, str] = {}
    if requirements:
        cache_key = "_shvya_memory_requirement_mappings"
        cached = getattr(lead, cache_key, None)
        if (
            isinstance(cached, Mapping)
            and cached.get("profile_revision") == profile.revision
            and isinstance(cached.get("mappings"), Mapping)
        ):
            mappings = dict(cached["mappings"])
        else:
            try:
                from apps.ai_engagement.services.qualification_execution_contract import (
                    _config,
                )

                config = _config(
                    organization=organization,
                    requirements=requirements,
                )
                mappings = {
                    str(key): str(value)
                    for key, value in (config.get("mappings") or {}).items()
                    if key and value
                }
                setattr(
                    lead,
                    cache_key,
                    {
                        "profile_revision": profile.revision,
                        "mappings": dict(mappings),
                    },
                )
            except Exception:
                # Failure must not make memory authoritative over CRM state.
                mappings = {}

    for requirement_id, item in (state.get("requirement_states") or {}).items():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("status") or "").casefold() != str(REQUIREMENT_ANSWERED).casefold():
            continue
        requirement_id = str(requirement_id or "").strip()
        if not requirement_id:
            continue
        mapped_key = mappings.get(requirement_id)
        if mapped_key and mapped_key in canonical:
            continue
        key = mapped_key or f"qualification.{requirement_id}"
        value = (
            lead_attributes.get(mapped_key)
            if mapped_key and mapped_key in lead_attributes
            else item.get("value")
        )
        canonical[key] = {
            "value": deepcopy(value),
            "confidence": 1.0,
            "source_message_id": item.get("source_message_id"),
            "source_type": (
                "verified_crm"
                if mapped_key and mapped_key in lead_attributes
                else "validated_qualification"
            ),
            "evidence": str(item.get("raw_answer") or "")[:2000],
            "updated_at": item.get("updated_at") or now,
        }

    return canonical, mappings


def _overlay_canonical(snapshot, canonical, *, max_facts):
    snapshot = deepcopy(snapshot if isinstance(snapshot, Mapping) else {})
    stored = snapshot.get("facts") if isinstance(snapshot.get("facts"), Mapping) else {}
    merged: dict[str, Any] = {}
    for key, value in canonical.items():
        if len(merged) >= max_facts:
            break
        merged[str(key)] = deepcopy(value)
    for key, value in stored.items():
        if str(key) in merged or len(merged) >= max_facts:
            continue
        merged[str(key)] = deepcopy(value)
    snapshot["facts"] = merged
    return snapshot


def _canonical_memory_mutation(*, key, old, raw, source_message_id, source_type, mutation_cls):
    proposed_source = str(raw.get("source_type") or source_type or "intent_fact").strip()
    proposed = {
        "value": deepcopy(raw.get("value")),
        "confidence": _bounded_confidence(raw.get("confidence")),
        "source_message_id": str(
            raw.get("source_message_id") or source_message_id or ""
        ).strip()
        or None,
        "source_type": proposed_source,
        "evidence": str(raw.get("evidence") or "").strip()[:2000],
        "updated_at": timezone.now().isoformat(),
    }
    same_value = old.get("value") == proposed.get("value")
    return mutation_cls(
        key=key,
        old=deepcopy(old),
        proposed=proposed,
        accepted=False,
        reason=(
            "canonical_backend_truth_same_value"
            if same_value
            else "canonical_backend_truth_conflict"
        ),
    )


def _remove_persisted_canonical_facts(*, organization, lead, canonical_keys, memory_key):
    if not canonical_keys:
        return
    from apps.crm.models import Lead

    with transaction.atomic():
        locked = (
            Lead.objects.select_for_update()
            .filter(pk=lead.pk, organization_id=organization.id)
            .first()
        )
        if locked is None:
            return
        attributes = deepcopy(locked.attributes or {})
        memory = attributes.get(memory_key)
        if not isinstance(memory, Mapping):
            return
        facts = dict(memory.get("facts") or {})
        changed = False
        for key in canonical_keys:
            if key in facts:
                facts.pop(key, None)
                changed = True
        if not changed:
            return
        memory = dict(memory)
        memory["facts"] = facts
        attributes[memory_key] = memory
        locked.attributes = attributes
        locked.save(update_fields=["attributes"])
        lead.attributes = deepcopy(attributes)


def _install_structured_memory_guard() -> None:
    from apps.ai_engagement.services import structured_memory as memory_module

    service_cls = memory_module.StructuredLeadMemoryService
    original_load = service_cls.load
    original_merge = service_cls.merge_facts
    original_should_accept = service_cls._should_accept

    @staticmethod
    def should_accept(*, old, proposed):
        if not isinstance(old, Mapping):
            return True, "new_fact"
        old_rank = _authority(old.get("source_type"))
        proposed_rank = _authority(proposed.get("source_type"))
        same_value = old.get("value") == proposed.get("value")
        if proposed_rank < old_rank:
            return False, (
                "lower_authority_duplicate"
                if same_value
                else "lower_authority_conflict"
            )
        if proposed_rank > old_rank:
            return True, (
                "higher_authority_refresh"
                if same_value
                else "higher_authority_correction"
            )
        return original_should_accept(old=old, proposed=proposed)

    @wraps(original_load)
    def load(self, *, organization, lead):
        snapshot = original_load(self, organization=organization, lead=lead)
        canonical, _ = _canonical_backend_facts(
            organization=organization,
            lead=lead,
        )
        return _overlay_canonical(
            snapshot,
            canonical,
            max_facts=self.MAX_FACTS,
        )

    @wraps(original_merge)
    def merge_facts(
        self,
        *,
        organization,
        lead,
        facts,
        source_message_id=None,
        source_type="intent_fact",
    ):
        canonical, mappings = _canonical_backend_facts(
            organization=organization,
            lead=lead,
        )
        passthrough = []
        canonical_mutations = []
        for raw in facts or ():
            if not isinstance(raw, Mapping):
                continue
            prepared = dict(raw)
            requirement_id = str(prepared.get("requirement_id") or "").strip()
            if requirement_id:
                prepared["key"] = (
                    mappings.get(requirement_id)
                    or f"qualification.{requirement_id}"
                )
            key = str(prepared.get("key") or prepared.get("name") or "").strip()
            if not key:
                continue
            prepared.setdefault("source_type", source_type)
            if key in canonical:
                canonical_mutations.append(
                    _canonical_memory_mutation(
                        key=key,
                        old=canonical[key],
                        raw=prepared,
                        source_message_id=source_message_id,
                        source_type=source_type,
                        mutation_cls=memory_module.MemoryMutation,
                    )
                )
                continue
            passthrough.append(prepared)

        if passthrough:
            _, mutations = original_merge(
                self,
                organization=organization,
                lead=lead,
                facts=passthrough,
                source_message_id=source_message_id,
                source_type=source_type,
            )
        else:
            mutations = []

        _remove_persisted_canonical_facts(
            organization=organization,
            lead=lead,
            canonical_keys=set(canonical),
            memory_key=memory_module.MEMORY_KEY,
        )
        snapshot = original_load(
            self,
            organization=organization,
            lead=lead,
        )
        snapshot = _overlay_canonical(
            snapshot,
            canonical,
            max_facts=self.MAX_FACTS,
        )
        return snapshot, [*mutations, *canonical_mutations]

    service_cls._should_accept = should_accept
    service_cls.load = load
    service_cls.merge_facts = merge_facts


def _working_hours_question(question: str) -> bool:
    text = str(question or "")
    # "Open appointment slots" is live availability, not opening hours.
    return bool(_WORKING_HOURS_RE.search(text)) and not bool(_LIVE_SLOT_RE.search(text))


def _install_live_availability_guard() -> None:
    from apps.ai_engagement.services import evidence_resolver as evidence_module
    from apps.ai_engagement.services.intent_types import Intent, IntentDecision
    from apps.ai_engagement.services.tenant_guard import TenantGuard

    resolver_cls = evidence_module.EvidenceResolver
    original = resolver_cls.resolve

    @wraps(original)
    def resolve(
        self,
        *,
        organization,
        lead,
        question,
        intent_decision=None,
        structured_memory=None,
    ):
        TenantGuard(organization).validate_lead(lead)
        intents = set()
        if isinstance(intent_decision, IntentDecision):
            intents = {
                intent_decision.primary_intent,
                *intent_decision.secondary_intents,
            }
        if Intent.AVAILABILITY_QUESTION in intents and not _working_hours_question(question):
            return evidence_module.EvidenceResolution(
                category=evidence_module.GroundingCategory.NO_VERIFIED_EVIDENCE,
                information_class=evidence_module.InformationClass.UNKNOWN,
                question_type="appointment_availability",
                sensitive=True,
                verified=False,
                evidence=(),
                controlled_fallback=(
                    "I don't have verified live appointment availability here, "
                    "so I don't want to guess."
                ),
            )
        return original(
            self,
            organization=organization,
            lead=lead,
            question=question,
            intent_decision=intent_decision,
            structured_memory=structured_memory,
        )

    resolver_cls.resolve = resolve


def _extractive_evidence_match(decision, resolution) -> bool:
    return matches_verified_evidence(
        decision, resolution, allow_scalar_price_template=True,
    )


def _low_risk_normal_reply(decision, resolution) -> bool:
    return is_social_only_reply(decision, resolution)


def _install_grounding_cost_guard() -> None:
    from apps.ai_engagement.graph import evidence as evidence_graph

    original = evidence_graph.check_grounding

    @wraps(original)
    def check_grounding(state):
        decision = state.get("decision")
        if decision is None or not getattr(decision, "should_engage", False):
            return original(state)
        try:
            from apps.ai_engagement.services.phase5_6_runtime import (
                current_evidence_resolution,
            )

            resolution = current_evidence_resolution(
                organization_id=getattr(state.get("organization"), "id", None),
                lead_id=getattr(state.get("lead"), "id", None),
            )
        except Exception:
            resolution = None
        if _extractive_evidence_match(decision, resolution):
            return {
                "grounding_approved": True,
                "grounding_validation_path": "deterministic_evidence_match",
            }
        if _low_risk_normal_reply(decision, resolution):
            return {
                "grounding_approved": True,
                "grounding_validation_path": "deterministic_low_risk",
            }
        return original(state)

    evidence_graph.check_grounding = check_grounding


def install_phase5_6_safety_fixes() -> None:
    """Close the Phase 5/6 audit gaps without creating parallel AI engines."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_live_availability_guard()
    _install_structured_memory_guard()
    _install_grounding_cost_guard()
    _INSTALLED = True


__all__ = ["install_phase5_6_safety_fixes"]

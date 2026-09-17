from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.services.tenant_guard import TenantGuard


MEMORY_KEY = "_shvya_ai_structured_memory_v1"
MEMORY_VERSION = 1


class StructuredMemoryError(RuntimeError):
    pass


class StructuredMemoryScopeError(StructuredMemoryError):
    pass


@dataclass(frozen=True)
class MemoryMutation:
    key: str
    old: Mapping[str, Any] | None
    proposed: Mapping[str, Any]
    accepted: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class StructuredLeadMemoryService:
    """Versioned fact memory physically owned by a tenant-scoped Lead row.

    Memory is never keyed or retrieved by phone number.  The embedded
    organization/lead identifiers are checked on every read and write so that
    copying an attributes payload across tenants fails closed instead of leaking
    customer facts.
    """

    MAX_FACTS = 100
    MAX_EVIDENCE_CHARS = 2000

    def load(self, *, organization, lead) -> dict[str, Any]:
        TenantGuard(organization).validate_lead(lead)
        return self._snapshot_from_attributes(
            organization=organization,
            lead=lead,
            attributes=getattr(lead, "attributes", None) or {},
        )

    def merge_facts(
        self,
        *,
        organization,
        lead,
        facts: Iterable[Mapping[str, Any]],
        source_message_id: str | None = None,
        source_type: str = "intent_fact",
    ) -> tuple[dict[str, Any], list[MemoryMutation]]:
        guard = TenantGuard(organization)
        guard.validate_lead(lead)

        from apps.crm.models import Lead

        with transaction.atomic():
            locked = (
                Lead.objects.select_for_update()
                .select_related("organization")
                .filter(pk=lead.pk, organization_id=organization.id)
                .first()
            )
            if locked is None:
                raise StructuredMemoryScopeError("Lead is outside the active organization scope.")

            snapshot = self._snapshot_from_attributes(
                organization=organization,
                lead=locked,
                attributes=locked.attributes or {},
            )
            stored = dict(snapshot.get("facts") or {})
            mutations: list[MemoryMutation] = []

            for raw in facts or ():
                normalized = self._normalize_fact(
                    raw,
                    source_message_id=source_message_id,
                    source_type=source_type,
                )
                if normalized is None:
                    continue
                key, proposed = normalized
                old = stored.get(key)
                accepted, reason = self._should_accept(old=old, proposed=proposed)
                mutations.append(
                    MemoryMutation(
                        key=key,
                        old=deepcopy(old) if isinstance(old, Mapping) else None,
                        proposed=deepcopy(proposed),
                        accepted=accepted,
                        reason=reason,
                    )
                )
                if accepted:
                    stored[key] = proposed

            if len(stored) > self.MAX_FACTS:
                ordered = sorted(
                    stored.items(),
                    key=lambda item: str((item[1] or {}).get("updated_at") or ""),
                    reverse=True,
                )[: self.MAX_FACTS]
                stored = dict(ordered)

            snapshot = {
                "version": MEMORY_VERSION,
                "organization_id": str(organization.id),
                "lead_id": str(locked.id),
                "facts": stored,
                "updated_at": timezone.now().isoformat(),
            }
            attributes = dict(locked.attributes or {})
            attributes[MEMORY_KEY] = snapshot
            locked.attributes = attributes
            locked.save(update_fields=["attributes"])

            # Keep the caller object coherent so a later save in the same turn
            # cannot accidentally overwrite the just-committed memory snapshot.
            lead.attributes = deepcopy(attributes)
            return deepcopy(snapshot), mutations

    def prompt_payload(self, snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        facts = snapshot.get("facts") if isinstance(snapshot.get("facts"), Mapping) else {}
        compact: dict[str, Any] = {}
        for key, item in facts.items():
            if not isinstance(item, Mapping):
                continue
            compact[str(key)] = {
                "value": item.get("value"),
                "confidence": item.get("confidence"),
                "source_message_id": item.get("source_message_id"),
                "source_type": item.get("source_type"),
                "evidence": item.get("evidence"),
            }
        return {
            "version": MEMORY_VERSION,
            "organization_id": snapshot.get("organization_id"),
            "lead_id": snapshot.get("lead_id"),
            "facts": compact,
            "authority": "backend_structured_lead_memory",
        }

    def trace_payload(
        self,
        *,
        snapshot: Mapping[str, Any] | None,
        mutations: Iterable[MemoryMutation] = (),
    ) -> dict[str, Any]:
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        facts = snapshot.get("facts") if isinstance(snapshot.get("facts"), Mapping) else {}
        return {
            "version": MEMORY_VERSION,
            "organization_id": snapshot.get("organization_id"),
            "lead_id": snapshot.get("lead_id"),
            "fact_keys": sorted(str(key) for key in facts.keys()),
            "mutations": [item.as_dict() for item in mutations],
            "authority": "backend_structured_lead_memory",
        }

    def _snapshot_from_attributes(self, *, organization, lead, attributes) -> dict[str, Any]:
        raw = attributes.get(MEMORY_KEY) if isinstance(attributes, Mapping) else None
        if not isinstance(raw, Mapping) or not raw:
            return {
                "version": MEMORY_VERSION,
                "organization_id": str(organization.id),
                "lead_id": str(lead.id),
                "facts": {},
                "updated_at": None,
            }

        stored_org = str(raw.get("organization_id") or "")
        stored_lead = str(raw.get("lead_id") or "")
        if stored_org != str(organization.id) or stored_lead != str(lead.id):
            raise StructuredMemoryScopeError("Structured memory tenant scope mismatch.")

        version = int(raw.get("version") or MEMORY_VERSION)
        if version != MEMORY_VERSION:
            raise StructuredMemoryError("Unsupported structured memory version.")

        facts = raw.get("facts")
        if not isinstance(facts, Mapping):
            facts = {}
        return {
            "version": MEMORY_VERSION,
            "organization_id": stored_org,
            "lead_id": stored_lead,
            "facts": deepcopy(dict(facts)),
            "updated_at": raw.get("updated_at"),
        }

    def _normalize_fact(
        self,
        raw: Mapping[str, Any],
        *,
        source_message_id: str | None,
        source_type: str,
    ) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(raw, Mapping):
            return None
        requirement_id = str(raw.get("requirement_id") or "").strip()
        raw_key = str(raw.get("key") or raw.get("name") or "").strip()
        key = raw_key or (f"qualification.{requirement_id}" if requirement_id else "")
        if not key or "value" not in raw:
            return None

        value = raw.get("value")
        if isinstance(value, (dict, list, tuple, set)):
            # Structured memory is deliberately scalar. Complex model output is
            # not allowed to become durable customer truth without another
            # deterministic schema owner.
            return None

        try:
            confidence = float(raw.get("confidence") if raw.get("confidence") is not None else 0.8)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))
        evidence = str(raw.get("evidence") or "").strip()[: self.MAX_EVIDENCE_CHARS]
        message_id = str(raw.get("source_message_id") or source_message_id or "").strip() or None
        fact_source_type = str(raw.get("source_type") or source_type or "intent_fact").strip()

        return key, {
            "value": value,
            "confidence": confidence,
            "source_message_id": message_id,
            "source_type": fact_source_type,
            "evidence": evidence,
            "updated_at": timezone.now().isoformat(),
        }

    @staticmethod
    def _should_accept(*, old, proposed: Mapping[str, Any]) -> tuple[bool, str]:
        if not isinstance(old, Mapping):
            return True, "new_fact"

        old_confidence = float(old.get("confidence") or 0.0)
        proposed_confidence = float(proposed.get("confidence") or 0.0)
        same_value = old.get("value") == proposed.get("value")

        if proposed_confidence < old_confidence:
            return False, (
                "lower_confidence_duplicate" if same_value else "lower_confidence_conflict"
            )
        if same_value:
            return True, "same_value_provenance_refresh"
        if proposed_confidence > old_confidence:
            return True, "higher_confidence_correction"
        return True, "equal_confidence_latest_explicit_fact"


__all__ = [
    "MEMORY_KEY",
    "MEMORY_VERSION",
    "MemoryMutation",
    "StructuredLeadMemoryService",
    "StructuredMemoryError",
    "StructuredMemoryScopeError",
]

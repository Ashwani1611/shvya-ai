from __future__ import annotations

from functools import wraps
from uuid import UUID


_INSTALLED = False


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


def install_canonical_architecture_compat() -> None:
    """Keep pure/synthetic AI contexts database-free.

    Production AI contexts contain a persisted UUID-backed CRM Lead, so canonical
    state reconciliation may resolve database truth. Unit tests, policy previews,
    prompt builders and isolated service tests may intentionally use synthetic IDs,
    mocks or unsaved leads. Those contexts must remain side-effect free and must
    not query CRM state merely because the canonical architecture is installed.

    This guard unwraps only the reconciliation decorators for non-persistent
    inputs. It does not change production retrieval, policy, execution or response
    validation behavior.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.engagement import EngagementService

    canonical_build_input = EngagementService._build_input
    pure_build_input = getattr(canonical_build_input, "__wrapped__", None)
    if pure_build_input is not None:

        @wraps(canonical_build_input)
        def persistent_context_only(self, *, context, **kwargs):
            if _persistent_lead_id(context) is None:
                return pure_build_input(self, context=context, **kwargs)
            return canonical_build_input(self, context=context, **kwargs)

        EngagementService._build_input = persistent_context_only

    canonical_engage = EngagementService.engage
    pure_engage = getattr(canonical_engage, "__wrapped__", None)
    if pure_engage is not None:

        @wraps(canonical_engage)
        def persistent_lead_only(
            self,
            *,
            organization,
            lead,
            knowledge_query=None,
            context=None,
        ):
            if not _is_persistent_lead(lead):
                return pure_engage(
                    self,
                    organization=organization,
                    lead=lead,
                    knowledge_query=knowledge_query,
                    context=context,
                )
            return canonical_engage(
                self,
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
                context=context,
            )

        EngagementService.engage = persistent_lead_only

    _INSTALLED = True

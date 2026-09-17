from __future__ import annotations

from contextvars import ContextVar
from functools import wraps

from apps.ai_engagement.services.organization_runtime_profile import (
    OrganizationAIRuntimeProfile,
    get_organization_ai_runtime_profile,
)
from apps.ai_engagement.services.tenant_guard import (
    TenantGuard,
    TenantScopeError,
)


_INSTALLED = False
_ACTIVE_PROFILE: ContextVar[OrganizationAIRuntimeProfile | None] = ContextVar(
    "shvya_organization_ai_runtime_profile",
    default=None,
)


def _record_profile_trace(profile: OrganizationAIRuntimeProfile) -> None:
    """Observability is fail-soft; tenant validation happened before this call."""
    try:
        from apps.ai_engagement.services.trace_service import record

        record(
            "runtime_profile",
            {
                "organization_id": profile.organization_id,
                "profile_version": profile.profile_version,
                "profile_revision": profile.revision,
                "tenant_validation": "passed",
            },
        )
    except Exception:
        return


def _record_tenant_failure(exc: TenantScopeError) -> None:
    try:
        from apps.ai_engagement.services.trace_service import record

        record(
            "tenant_validation",
            {
                "result": "failed",
                "code": exc.code,
                "object_type": exc.object_type,
            },
        )
    except Exception:
        return


def _patch_context_builder() -> None:
    from apps.ai_engagement.services import context as context_module

    original_build = context_module.AIContextBuilder.build
    original_org_context = context_module.AIContextBuilder._build_organization_context

    @wraps(original_org_context)
    def organization_context(self, *, organization):
        profile = _ACTIVE_PROFILE.get()
        if profile is None or profile.organization_id != str(organization.id):
            profile = get_organization_ai_runtime_profile(
                organization=organization,
            )
        return profile.legacy_organization_context()

    @wraps(original_build)
    def build(self, *args, **kwargs):
        organization = kwargs.get("organization")
        lead = kwargs.get("lead")
        try:
            guard = TenantGuard(organization)
            guard.validate_current_lead_context(lead)
            profile = get_organization_ai_runtime_profile(
                organization=organization,
                lead=lead,
            )
        except TenantScopeError as exc:
            _record_tenant_failure(exc)
            # Keep the pre-Phase-4 public error contract for a mismatched lead.
            # TenantGuard still fails closed first and the internal trace retains
            # the safe tenant code without exposing foreign organization details.
            if exc.object_type == "lead":
                raise context_module.AIContextError(
                    "Lead does not belong to this organization."
                ) from exc
            raise context_module.AIContextError(exc.code) from exc

        token = _ACTIVE_PROFILE.set(profile)
        try:
            result = original_build(self, *args, **kwargs)
        finally:
            _ACTIVE_PROFILE.reset(token)

        _record_profile_trace(profile)
        return result

    context_module.AIContextBuilder._build_organization_context = organization_context
    context_module.AIContextBuilder.build = build


def _patch_qualification_runtime() -> None:
    from apps.ai_engagement.services import transactional_turn_runtime as turn_runtime
    from apps.ai_engagement.services.qualification_state import requirements_for_lead

    def requirements_for_turn(*, organization, lead):
        profile = get_organization_ai_runtime_profile(
            organization=organization,
            lead=lead,
        )
        configured = profile.configured_requirements()
        return requirements_for_lead(lead, configured)

    turn_runtime._requirements_for_turn = requirements_for_turn


def _validate_retrieved(*, organization, results):
    guard = TenantGuard(organization)
    for item in results or []:
        guard.validate_chunk(getattr(item, "chunk", None))
    return results


def _patch_retrieval() -> None:
    from apps.ai_engagement.services import retrieval as retrieval_module

    for method_name in (
        "retrieve_by_vector",
        "retrieve_by_keyword",
        "retrieve_hybrid",
    ):
        original = getattr(retrieval_module.KnowledgeRetrievalService, method_name)

        @wraps(original)
        def guarded(self, *args, __original=original, **kwargs):
            organization = kwargs.get("organization")
            try:
                results = __original(self, *args, **kwargs)
                return _validate_retrieved(
                    organization=organization,
                    results=results,
                )
            except TenantScopeError as exc:
                _record_tenant_failure(exc)
                raise retrieval_module.RetrievalError(exc.code) from exc

        setattr(
            retrieval_module.KnowledgeRetrievalService,
            method_name,
            guarded,
        )


def _patch_crm_executor() -> None:
    from apps.ai_engagement.services import crm_executor as executor_module
    from apps.ai_engagement.services.crm_actions import (
        CRMActionSchemaError,
        validate_crm_actions,
    )

    original = executor_module.CRMActionExecutor.execute

    @wraps(original)
    def execute(self, *args, **kwargs):
        organization = kwargs.get("organization")
        lead = kwargs.get("lead")
        actions = kwargs.get("actions")
        current_action = None
        try:
            guard = TenantGuard(organization)
            guard.validate_current_lead_context(lead)
            try:
                normalized = validate_crm_actions(actions)
            except CRMActionSchemaError:
                # Preserve the canonical executor's existing schema error wording.
                return original(self, *args, **kwargs)
            for action in normalized:
                current_action = action
                guard.validate_crm_action(
                    lead=lead,
                    action=action,
                )
        except TenantScopeError as exc:
            _record_tenant_failure(exc)
            # The canonical executor already used a tenant-safe, non-disclosing
            # stage error. Preserve that public contract while TenantGuard still
            # blocks the foreign target before any mutation service is reached.
            if (
                isinstance(current_action, dict)
                and current_action.get("type") == "pipeline_transition"
                and exc.object_type in {"pipeline", "stage"}
            ):
                raise executor_module.CRMActionExecutionError(
                    "Requested stage does not belong to an active "
                    "pipeline in this organization."
                ) from exc
            raise executor_module.CRMActionExecutionError(exc.code) from exc
        return original(self, *args, **kwargs)

    executor_module.CRMActionExecutor.execute = execute


def _patch_ai_permissions() -> None:
    from apps.ai_engagement.services import ai_permissions as permission_module

    original = permission_module.AIPermissionService.evaluate

    @wraps(original)
    def evaluate(self, *args, **kwargs):
        organization = kwargs.get("organization")
        lead = kwargs.get("lead")
        latest_inbound = kwargs.get("latest_inbound")
        try:
            guard = TenantGuard(organization)
            guard.validate_current_lead_context(lead)
            if latest_inbound is not None:
                guard.validate_message(
                    latest_inbound,
                    lead=lead,
                    account=getattr(latest_inbound, "account", None),
                )
        except TenantScopeError as exc:
            _record_tenant_failure(exc)
            raise permission_module.AIPermissionError(exc.code) from exc
        return original(self, *args, **kwargs)

    permission_module.AIPermissionService.evaluate = evaluate


def _patch_trace_service() -> None:
    from apps.ai_engagement.services import trace_service

    original = trace_service.begin_trace

    @wraps(original)
    def begin_trace(*, organization, lead=None, source_message=None, account=None):
        # This precondition is intentionally OUTSIDE Phase 1's fail-soft trace
        # creation try/except. Observability may fail soft; tenant security may not.
        guard = TenantGuard(organization)
        try:
            if lead is not None:
                guard.validate_current_lead_context(lead)
            if account is not None:
                guard.validate_whatsapp_account(account)
            if source_message is not None:
                guard.validate_message(
                    source_message,
                    lead=lead,
                    account=account,
                )
        except TenantScopeError as exc:
            _record_tenant_failure(exc)
            raise
        return original(
            organization=organization,
            lead=lead,
            source_message=source_message,
            account=account,
        )

    trace_service.begin_trace = begin_trace


def _patch_file_sharing() -> None:
    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services import file_sharing as sharing_module

    original = sharing_module.FileSharingService.get_eligible_documents

    @wraps(original)
    def get_eligible_documents(
        self,
        *,
        organization,
        document_ids=None,
    ):
        guard = TenantGuard(organization)
        try:
            if document_ids is not None:
                supplied_ids = list(document_ids)
                # Inspect only ownership metadata. A missing ID remains simply
                # unavailable; an existing foreign ID is an explicit scope failure.
                for document in Document.objects.filter(
                    id__in=supplied_ids
                ).only("id", "organization_id"):
                    guard.validate_document(document)

            documents = original(
                self,
                organization=organization,
                document_ids=(set(supplied_ids) if document_ids is not None else None),
            )
            for document in documents:
                guard.validate_file(document)
            return documents
        except TenantScopeError as exc:
            _record_tenant_failure(exc)
            # Preserve the canonical non-disclosing eligibility contract. The
            # tenant mismatch remains available in internal trace metadata.
            raise sharing_module.FileSharingError(
                "AI selected a document that is not an eligible "
                "organization-owned file."
            ) from exc

    sharing_module.FileSharingService.get_eligible_documents = get_eligible_documents


def install_phase4_runtime() -> None:
    """Install the organization-runtime/tenant-security boundary once.

    This keeps existing engines, ranking, qualification policy, delivery and CRM
    mutation services authoritative. Phase 4 only centralizes configuration
    assembly and adds fail-closed validation before foreign references can enter
    AI/model/mutation paths.
    """

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _patch_context_builder()
    _patch_qualification_runtime()
    _patch_retrieval()
    _patch_crm_executor()
    _patch_ai_permissions()
    _patch_trace_service()
    _patch_file_sharing()

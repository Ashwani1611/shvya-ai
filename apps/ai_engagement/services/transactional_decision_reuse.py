from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace


logger = logging.getLogger(__name__)
_INSTALLED = False

# A file share is a state-changing customer action even though the physical
# WhatsApp document is queued only after final wording exists. Resolve and audit
# the selected organization file before the final response is generated, then
# force the send path to use that exact resolved selection.
_PRE_RESOLVED_FILE_KEY = "pre_resolved_file_document_id"
_PRE_RESOLVED_FILE_STATUS_KEY = "pre_resolved_file_status"
_POST_STATE_REVISION_PREFIX = "post-state-regenerate:"
_PENDING_FILE_RESOLUTION: ContextVar[dict | None] = ContextVar(
    "shvya_pending_file_resolution",
    default=None,
)


def _resolve_file_document_id(*, organization, decision) -> int | None:
    selected = getattr(decision, "file_document_id", None)
    if selected is None:
        return None
    if isinstance(selected, bool):
        raise ValueError("Engagement decision contains an invalid file_document_id.")
    try:
        document_id = int(selected)
    except (TypeError, ValueError) as exc:
        raise ValueError("Engagement decision contains an invalid file_document_id.") from exc
    if document_id <= 0:
        raise ValueError("Engagement decision contains an invalid file_document_id.")

    from apps.ai_engagement.services.file_sharing import FileSharingService

    eligible = FileSharingService().get_eligible_documents(
        organization=organization,
        document_ids={document_id},
    )
    if not any(int(document.id) == document_id for document in eligible):
        raise ValueError(
            "Engagement decision selected a file that is not currently eligible "
            "for AI-guided sharing."
        )
    return document_id


def _pending_post_state_turn(*, runtime, lead) -> tuple[dict, dict] | None:
    cached = runtime._PRECOMPUTED_DECISION.get()
    if not isinstance(cached, dict) or not cached.get("force_regenerate"):
        return None
    if str(cached.get("lead_id") or "") != str(getattr(lead, "pk", "")):
        return None

    state = runtime._runtime_state(lead)
    source_id = str(cached.get("source_message_id") or "")
    if not source_id or str(state.get(runtime._PRE_RESOLVED_MESSAGE_KEY) or "") != source_id:
        return None

    inbound = (
        lead.whatsapp_messages.filter(
            id=source_id,
            organization_id=lead.organization_id,
            direction="inbound",
        )
        .only("raw_payload")
        .first()
    )
    if inbound is None:
        return None
    payload = inbound.raw_payload if isinstance(inbound.raw_payload, dict) else {}
    processing = payload.get("shvya_ai_processing")
    processing = processing if isinstance(processing, dict) else {}
    if not processing.get("state_resolved") or processing.get("processed"):
        return None
    return state, processing


def install_transactional_decision_reuse() -> None:
    """Make state-changing turns two-pass and final-response-only after commit.

    Pass 1 is an understanding/action-proposal pass. It may propose evidence-
    backed attribute, qualification, stage, reminder/contact/note and file-share
    actions. The transactional runtime resolves those actions first.

    Pass 2 MUST be a fresh engagement generation from the committed backend
    state. The pre-mutation customer-facing text is deliberately discarded. The
    final pass is language-only: already-resolved qualification/CRM actions are
    removed, and a resolved file selection is forced from backend state rather
    than letting the model change it after resolution.

    Non-state-changing turns may still reuse their single validated decision.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import transactional_turn_runtime as runtime
    from apps.ai_engagement.services.runtime_state import STATE_KEY, state_revision

    # ------------------------------------------------------------
    # Treat a selected file as a state-changing action.
    # ------------------------------------------------------------
    current_state_changing = runtime._state_changing_decision

    def state_changing_decision(decision) -> bool:
        return current_state_changing(decision) or getattr(
            decision, "file_document_id", None
        ) is not None

    runtime._state_changing_decision = state_changing_decision

    # ------------------------------------------------------------
    # Persist the resolved file choice inside the same transaction that marks
    # attributes/qualification/stage/reminder actions resolved.
    # ------------------------------------------------------------
    current_mark_state_resolved = runtime._mark_state_resolved

    def mark_state_resolved(*, lead, inbound, action_types):
        pending = _PENDING_FILE_RESOLUTION.get()
        matches_turn = bool(
            isinstance(pending, dict)
            and str(pending.get("lead_id") or "") == str(getattr(lead, "pk", ""))
            and str(pending.get("source_message_id") or "") == str(getattr(inbound, "pk", ""))
        )
        resolved_file_id = pending.get("document_id") if matches_turn else None
        types = list(action_types or [])
        if matches_turn and resolved_file_id is not None and "file_share" not in types:
            types.append("file_share")

        current_mark_state_resolved(
            lead=lead,
            inbound=inbound,
            action_types=types,
        )

        if not matches_turn:
            return

        # The base marker has already refreshed and persisted the runtime state.
        # Extend it without replacing unrelated qualification/runtime metadata.
        attributes = deepcopy(lead.attributes) if isinstance(lead.attributes, dict) else {}
        state = deepcopy(attributes.get(STATE_KEY)) if isinstance(attributes.get(STATE_KEY), dict) else {}
        if resolved_file_id is None:
            state.pop(_PRE_RESOLVED_FILE_KEY, None)
            state.pop(_PRE_RESOLVED_FILE_STATUS_KEY, None)
        else:
            state[_PRE_RESOLVED_FILE_KEY] = int(resolved_file_id)
            state[_PRE_RESOLVED_FILE_STATUS_KEY] = "resolved_pending_send"
        attributes[STATE_KEY] = state
        lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
        lead.attributes = attributes

        payload = deepcopy(inbound.raw_payload) if isinstance(inbound.raw_payload, dict) else {}
        processing = payload.get("shvya_ai_processing")
        processing = deepcopy(processing) if isinstance(processing, dict) else {}
        if resolved_file_id is None:
            processing.pop("resolved_file_document_id", None)
            processing.pop("file_share_status", None)
        else:
            processing["resolved_file_document_id"] = int(resolved_file_id)
            processing["file_share_status"] = "resolved_pending_send"
        payload["shvya_ai_processing"] = processing
        inbound.raw_payload = payload
        inbound.save(update_fields=["raw_payload", "updated_at"])

    runtime._mark_state_resolved = mark_state_resolved

    # ------------------------------------------------------------
    # Resolve file eligibility before any state mutation, run the transactional
    # mutation pass, then mark the canonical pass as FORCE REGENERATE. We keep a
    # one-turn marker for the destination-stage permission exception, but never
    # cache/reuse the pre-mutation response candidate.
    # ------------------------------------------------------------
    current_resolve = runtime._resolve_state_before_response

    def resolve_state_before_response(
        *,
        organization,
        lead,
        source_message_id,
        decision,
        account_id=None,
    ):
        resolved_file_id = _resolve_file_document_id(
            organization=organization,
            decision=decision,
        )
        token = _PENDING_FILE_RESOLUTION.set(
            {
                "lead_id": str(getattr(lead, "pk", "")),
                "source_message_id": str(source_message_id or ""),
                "document_id": resolved_file_id,
            }
        )
        try:
            result = current_resolve(
                organization=organization,
                lead=lead,
                source_message_id=source_message_id,
                decision=decision,
                account_id=account_id,
            )
        finally:
            _PENDING_FILE_RESOLUTION.reset(token)

        if not isinstance(result, dict) or not result.get("applied"):
            return result

        lead.refresh_from_db(fields=["attributes", "pipeline", "stage"])
        committed_revision = state_revision(lead)
        runtime._PRECOMPUTED_DECISION.set(
            {
                "lead_id": str(lead.pk),
                "source_message_id": str(source_message_id or ""),
                # Deliberately never equals state_revision(lead), so the cached
                # engagement wrapper cannot return the draft response.
                "revision": f"{_POST_STATE_REVISION_PREFIX}{committed_revision}",
                "decision": None,
                "force_regenerate": True,
            }
        )
        return {
            **result,
            "file_document_id": resolved_file_id,
            "committed_revision": committed_revision,
            "final_response_requires_regeneration": True,
        }

    runtime._resolve_state_before_response = resolve_state_before_response

    # ------------------------------------------------------------
    # Final engagement pass: build fresh context from committed state, then make
    # it language-only. Backend-resolved file selection is authoritative.
    # ------------------------------------------------------------
    from apps.ai_engagement.services.engagement import EngagementService

    current_engage = EngagementService.engage

    def engage_from_committed_state(
        self,
        *,
        organization,
        lead,
        knowledge_query=None,
        context=None,
    ):
        decision = current_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
        pending = _pending_post_state_turn(runtime=runtime, lead=lead)
        if pending is None:
            return decision
        state, _processing = pending
        resolved_file_id = state.get(_PRE_RESOLVED_FILE_KEY)
        try:
            resolved_file_id = int(resolved_file_id) if resolved_file_id is not None else None
        except (TypeError, ValueError):
            resolved_file_id = None
        return replace(
            decision,
            crm_actions=[],
            qualification_updates=[],
            file_document_id=resolved_file_id,
        )

    EngagementService.engage = engage_from_committed_state

    # ------------------------------------------------------------
    # Put verified operational state into the generation payload. Stage,
    # qualification and attributes were already present; reminders and a
    # resolved file action were not. This makes the fresh final generation truly
    # reflect all persisted operations rather than prompt-only intentions.
    # ------------------------------------------------------------
    current_build_input = EngagementService._build_input

    def build_input_with_operational_state(self, *, context, **kwargs):
        raw = current_build_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw

        lead_data = context.lead if isinstance(context.lead, dict) else {}
        lead_id = str(lead_data.get("id") or "").strip()
        if not lead_id:
            return raw

        from apps.crm.models import LeadReminder

        reminders = list(
            LeadReminder.objects.filter(
                lead_id=lead_id,
                status="pending",
            )
            .order_by("due_at", "created_at")
            .values("title", "description", "due_at", "status")[:10]
        )
        for reminder in reminders:
            due_at = reminder.get("due_at")
            if due_at is not None:
                reminder["due_at"] = due_at.isoformat()

        attributes = lead_data.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        state = attributes.get(STATE_KEY)
        state = state if isinstance(state, dict) else {}
        resolved_actions = {
            "source_message_id": state.get(runtime._PRE_RESOLVED_MESSAGE_KEY),
            "action_types": state.get(runtime._PRE_RESOLVED_ACTIONS_KEY) or [],
        }
        resolved_file_id = state.get(_PRE_RESOLVED_FILE_KEY)
        if resolved_file_id is not None:
            from apps.ai_engagement.models import Document

            document = (
                Document.objects.filter(
                    id=resolved_file_id,
                    organization_id=getattr(context, "organization", {}).get("id"),
                )
                .only("name")
                .first()
            )
            resolved_actions["file_share"] = {
                "status": state.get(_PRE_RESOLVED_FILE_STATUS_KEY) or "resolved_pending_send",
                "document_name": document.name if document is not None else "configured file",
            }

        payload["operational_state"] = {
            "pending_reminders": reminders,
            "resolved_actions": resolved_actions,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    EngagementService._build_input = build_input_with_operational_state

    # ------------------------------------------------------------
    # A deterministic transition can land on a stage whose AI is disabled. That
    # stage blocks future turns, but not the fresh final acknowledgement for the
    # exact inbound message that caused the transition.
    # ------------------------------------------------------------
    from apps.ai_engagement.services.ai_permissions import AIPermissionService

    current_permission_evaluate = AIPermissionService.evaluate

    def evaluate_permission(self, *, organization, lead, latest_inbound=None):
        permission = current_permission_evaluate(
            self,
            organization=organization,
            lead=lead,
            latest_inbound=latest_inbound,
        )
        if permission.allowed or permission.reason != "stage_ai_disabled":
            return permission

        cached = runtime._PRECOMPUTED_DECISION.get()
        if (
            not isinstance(cached, dict)
            or not cached.get("force_regenerate")
            or cached.get("lead_id") != str(getattr(lead, "pk", ""))
        ):
            return permission

        if latest_inbound is None:
            latest_inbound = self._latest_inbound_message(
                organization=organization,
                lead=lead,
            )
        source_message_id = str(cached.get("source_message_id") or "")
        if not source_message_id or str(getattr(latest_inbound, "pk", "") or "") != source_message_id:
            return permission

        if not getattr(lead, "ai_enabled", True):
            return self._decision(
                allowed=False,
                reason="lead_ai_disabled",
                organization=organization,
                lead=lead,
            )
        mapping_allowed, mapping_reason = self._conversation_uses_pipeline_number(
            organization=organization,
            lead=lead,
            latest_message=latest_inbound,
        )
        if not mapping_allowed:
            return self._decision(
                allowed=False,
                reason=mapping_reason,
                organization=organization,
                lead=lead,
            )
        return self._decision(
            allowed=True,
            reason="same_turn_stage_transition_finalization",
            organization=organization,
            lead=lead,
        )

    AIPermissionService.evaluate = evaluate_permission

    # ------------------------------------------------------------
    # Finalization previously replaced shvya_ai_processing wholesale, erasing
    # the pre-resolution/file audit fields. Preserve them while allowing final
    # processed/hash values to win.
    # ------------------------------------------------------------
    from apps.ai_engagement import tasks as task_module

    current_persist_answers = task_module._persist_engagement_answers

    def persist_answers_with_resolution_audit(lead, decision, source_message_id):
        inbound = (
            lead.whatsapp_messages.filter(
                pk=source_message_id,
                organization_id=lead.organization_id,
                direction="inbound",
            )
            .only("raw_payload")
            .first()
        )
        before_payload = (
            deepcopy(inbound.raw_payload)
            if inbound is not None and isinstance(inbound.raw_payload, dict)
            else {}
        )
        before_processing = before_payload.get("shvya_ai_processing")
        before_processing = (
            deepcopy(before_processing) if isinstance(before_processing, dict) else {}
        )

        persisted = current_persist_answers(lead, decision, source_message_id)
        if not persisted or not before_processing:
            return persisted

        inbound = lead.whatsapp_messages.select_for_update().get(
            pk=source_message_id,
            organization_id=lead.organization_id,
            direction="inbound",
        )
        payload = deepcopy(inbound.raw_payload) if isinstance(inbound.raw_payload, dict) else {}
        after_processing = payload.get("shvya_ai_processing")
        after_processing = deepcopy(after_processing) if isinstance(after_processing, dict) else {}
        payload["shvya_ai_processing"] = {
            **before_processing,
            **after_processing,
        }
        inbound.raw_payload = payload
        inbound.save(update_fields=["raw_payload", "updated_at"])
        return persisted

    task_module._persist_engagement_answers = persist_answers_with_resolution_audit

    # ContextVars survive until explicitly cleared. Always clear the one-turn
    # regeneration marker even when canonical execution exits early.
    current_task_execute = task_module._execute_ai_engagement_response_impl

    def execute_task(*, task, lead_id: str):
        try:
            return current_task_execute(task=task, lead_id=lead_id)
        finally:
            runtime._PRECOMPUTED_DECISION.set(None)

    task_module._execute_ai_engagement_response_impl = execute_task

    from apps.hosted_automation import execution as hosted_execution

    current_hosted_execute = hosted_execution.execute_hosted_ai_engagement

    def execute_hosted(*, task, job):
        try:
            return current_hosted_execute(task=task, job=job)
        finally:
            runtime._PRECOMPUTED_DECISION.set(None)

    hosted_execution.execute_hosted_ai_engagement = execute_hosted
    _INSTALLED = True

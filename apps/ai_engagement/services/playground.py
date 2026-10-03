from __future__ import annotations

import logging
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from time import monotonic
from types import SimpleNamespace
from typing import Any

from django.core.cache import cache

from apps.ai_engagement.services.ai_provider import OpenAIProvider
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.embeddings import EmbeddingError, EmbeddingService
from apps.ai_engagement.services.engagement import (
    EngagementDecision,
    EngagementError,
    EngagementService,
)
from apps.ai_engagement.services.engagement_failsoft import (
    build_deterministic_fallback_decision,
)
from apps.ai_engagement.services.first_inbound_welcome_runtime import (
    apply_first_inbound_welcome,
)
from apps.ai_engagement.services.org_info import OrgInfoService
from apps.ai_engagement.services.organization_profile import (
    compile_org_ai_profile_from_context,
)
from apps.ai_engagement.services.qualification_state import (
    attributes_with_state,
    project_answer_updates,
    record_last_asked_requirement,
    requirements_for_lead,
    state_for_lead,
)
from apps.ai_engagement.services.playground_finalization import (
    language_only_decision,
    needs_final_composition,
    preserve_preview_state,
    resolved_preview_actions,
)
from apps.ai_engagement.services.retrieval import KnowledgeRetrievalService
from apps.ai_engagement.services.runtime_state import (
    STATE_KEY,
    contract,
    observe_message,
    response_hash,
)


logger = logging.getLogger(__name__)


class _SandboxLead(SimpleNamespace):
    """In-memory lead compatible with the production qualification runtime."""

    def _persist_qualification_state(self, attributes):
        self.attributes = deepcopy(attributes)


class _SandboxContextBuilder:
    """Build production-shaped AIContext objects without creating a CRM lead.

    The public EngagementService still owns routing, qualification extraction,
    RAG decisions, validation, grounding, schema repair and fail-soft behavior.
    This builder only supplies the same shape of state that a real WhatsApp lead
    would expose to that runtime.
    """

    def __init__(
        self,
        *,
        organization,
        visitor,
        conversation: list[dict[str, Any]],
        org_info_service: OrgInfoService,
        embedding_service: EmbeddingService,
        retrieval_service: KnowledgeRetrievalService,
        pipeline=None,
        stage=None,
        channel: str = "sandbox",
    ) -> None:
        self.organization = organization
        self.visitor = visitor
        self.conversation = conversation
        self.org_info_service = org_info_service
        self.embedding_service = embedding_service
        self.retrieval_service = retrieval_service
        self.pipeline = pipeline
        self.stage = stage
        self.channel = channel
        self.last_knowledge: list[dict[str, Any]] = []

    def organization_context(self) -> dict[str, Any]:
        org_info = self.org_info_service.get_or_create(organization=self.organization)
        return {
            "id": str(self.organization.id),
            "name": self.organization.name,
            "ai_enabled": org_info.ai_enabled,
            "about": org_info.about,
            "bot_languages": org_info.bot_languages,
            "ai_playbook": org_info.ai_playbook,
            "bump_up_enabled": org_info.bump_up_enabled,
            "bump_up_count": org_info.bump_up_count,
        }

    def _pipeline_context(self) -> dict[str, Any]:
        pipeline = self.pipeline
        if pipeline is None:
            return {}

        from apps.ai_engagement.services.confidentiality import (
            is_sensitive_attribute_definition,
        )

        try:
            attribute_definitions = list(
                self.organization.crm_attribute_definitions.exclude(key="booked_at").values(
                    "key",
                    "name",
                    "field_type",
                    "description",
                    "options",
                )
            )
        except Exception:
            attribute_definitions = []

        try:
            stages = [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "description": item.description,
                    "config": item.config,
                }
                for item in pipeline.stages.filter(is_active=True).order_by(
                    "display_order",
                    "name",
                )
            ]
        except Exception:
            stages = []

        return {
            "id": str(pipeline.id),
            "name": pipeline.name,
            "description": pipeline.description,
            "country_code": pipeline.country_code,
            "phone_number": pipeline.phone_number,
            "is_active": pipeline.is_active,
            "available_stages": stages,
            "attribute_definitions": [
                item for item in attribute_definitions
                if not is_sensitive_attribute_definition(item)
            ],
        }

    def _stage_context(self) -> dict[str, Any]:
        stage = self.stage
        if stage is None:
            return {"name": "New Lead"}
        return {
            "id": str(stage.id),
            "name": stage.name,
            "description": stage.description,
            "display_order": stage.display_order,
            "color": stage.color,
            "is_active": stage.is_active,
            "config": stage.config,
        }

    def _attribute_context(self) -> list[dict[str, Any]]:
        from apps.ai_engagement.services.confidentiality import safe_attribute_values

        attributes = safe_attribute_values(self.visitor.attributes)
        return [
            {"name": str(name), "value": value}
            for name, value in attributes.items()
        ]

    def _retrieve_knowledge(
        self,
        *,
        organization,
        knowledge_query: str | None,
        query_vector: list[float] | None,
        knowledge_limit: int,
    ) -> list[dict[str, Any]]:
        vector = query_vector
        query = str(knowledge_query or "").strip()
        if vector is None and not query:
            return []

        if vector is None:
            try:
                vector = self.embedding_service.embed_text(
                    query,
                    organization_id=organization.id,
                    feature="playground_knowledge_query",
                    reference_id=str(organization.id),
                )
            except EmbeddingError:
                logger.info(
                    "ai_sandbox_embedding_unavailable organization=%s",
                    getattr(organization, "id", ""),
                )
                vector = None

        try:
            limit = min(max(int(knowledge_limit or 3), 1), 20)
            results = self.retrieval_service.retrieve_hybrid(
                organization=organization,
                query_text=query,
                query_vector=vector,
                limit=limit,
            )
        except Exception:
            logger.exception(
                "AI Sandbox knowledge retrieval failed for organization %s",
                getattr(organization, "id", ""),
            )
            return []

        return [
            {
                "chunk_id": str(result.chunk.id),
                "document_id": str(result.chunk.document_id),
                "document_name": result.chunk.document.name,
                "document_version": result.chunk.document.version,
                "content": result.chunk.content,
                "similarity": result.similarity,
                "distance": result.distance,
            }
            for result in results
        ]

    def build(
        self,
        *,
        organization,
        lead,
        knowledge_query: str | None = None,
        query_vector: list[float] | None = None,
        message_limit: int = 50,
        knowledge_limit: int = 5,
        note_limit: int = 20,
    ) -> AIContext:
        if str(getattr(organization, "id", "")) != str(self.organization.id):
            raise EngagementError("AI context organization does not match request.")
        if str(getattr(lead, "id", "")) != str(self.visitor.id):
            raise EngagementError("AI context lead does not match request.")

        knowledge = self._retrieve_knowledge(
            organization=organization,
            knowledge_query=knowledge_query,
            query_vector=query_vector,
            knowledge_limit=knowledge_limit,
        )
        if knowledge_query or query_vector is not None:
            self.last_knowledge = list(knowledge)

        recent_conversation = list(self.conversation[-max(int(message_limit or 1), 1) :])
        visitor_attributes = (
            self.visitor.attributes
            if isinstance(self.visitor.attributes, dict)
            else {}
        )

        return AIContext(
            organization=self.organization_context(),
            lead={
                "id": str(self.visitor.id),
                "name": getattr(self.visitor, "name", "Playground Visitor"),
                "phone": "",
                "email": "",
                "notes": "",
                "attributes": visitor_attributes,
                "qualification": state_for_lead(self.visitor),
                "lead_source": getattr(self.visitor, "lead_source", "system"),
                "operational_state": {
                    "execution_mode": "sandbox_preview",
                    "reminder": deepcopy(getattr(self.visitor, "preview_reminder", None)),
                },
                "shared_document_ids": list(getattr(self.visitor, "shared_document_ids", [])),
                "stage_entered_at": None,
                "created_at": None,
                "updated_at": None,
            },
            pipeline=self._pipeline_context(),
            stage=self._stage_context(),
            contacts=[],
            attributes=self._attribute_context(),
            conversation={
                "channel": self.channel,
                "execution_mode": "sandbox_preview",
                "message_count": len(recent_conversation),
                "messages": recent_conversation,
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=knowledge,
        )

    def latest_inbound_for_fallback(self, *, organization, lead):
        """Let production fail-soft use this sandbox turn instead of ORM messages."""
        for message in reversed(self.conversation):
            if message.get("direction") == "inbound" and str(message.get("body") or "").strip():
                return SimpleNamespace(body=str(message["body"]).strip())
        return None


class PlaygroundError(Exception):
    """Raised when the AI Playground request cannot be completed safely."""


@dataclass(frozen=True)
class PlaygroundResult:
    """Normalized result for one Playground turn."""

    session_id: str
    message: str
    response: str
    should_engage: bool
    knowledge: list[dict[str, Any]]
    model: str
    stage: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    files: list = field(default_factory=list)
    channel: str = "sandbox"
    lead_source: str = "system"

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message": self.message,
            "response": self.response,
            "should_engage": self.should_engage,
            "knowledge": self.knowledge,
            "model": self.model,
            "stage": self.stage, "events": self.events, "files": self.files,
            "channel": self.channel, "lead_source": self.lead_source,
            "execution_mode": "sandbox_preview",
        }


class PlaygroundService:
    """Isolated AI Sandbox backed by the production engagement runtime."""

    MAX_HISTORY_MESSAGES = 40
    MAX_MESSAGE_LENGTH = 4000
    MAX_KNOWLEDGE = 5
    SESSION_TTL_SECONDS = 24 * 60 * 60

    def __init__(
        self,
        *,
        provider: OpenAIProvider | None = None,
        engagement_service: EngagementService | None = None,
        org_info_service: OrgInfoService | None = None,
        embedding_service: EmbeddingService | None = None,
        retrieval_service: KnowledgeRetrievalService | None = None,
    ) -> None:
        self.provider = provider
        self.engagement_service = engagement_service
        self.org_info_service = org_info_service or OrgInfoService()
        self.embedding_service = embedding_service or EmbeddingService()
        self.retrieval_service = retrieval_service or KnowledgeRetrievalService()

    def run(
        self,
        *,
        organization,
        session_id: str,
        message: str,
        history: list[dict[str, Any]] | None = None,
        stage_id: str | None = None,
        channel: str | None = None,
        lead_source: str | None = None,
    ) -> PlaygroundResult:
        started_at = monotonic()
        if organization is None:
            raise PlaygroundError("Organization is required.")

        session_id = str(session_id or "").strip()
        if not session_id:
            raise PlaygroundError("session_id is required.")

        message = self._normalize_message(message)
        saved = self._load_session_payload(
            organization=organization,
            session_id=session_id,
        )
        channel, lead_source = self._session_context(
            saved=saved, channel=channel, lead_source=lead_source,
        )
        stored_history = (
            self._normalize_role_history(saved.get("history"))
            if "history" in saved
            else None
        )
        source_history = (
            stored_history
            if stored_history is not None
            else self._normalize_role_history(history)
        )
        conversation = self._normalize_history(
            history=source_history,
            current_message=message,
            session_id=session_id,
        )
        turn = int(saved.get("turn", 0)) + 1
        conversation[-1]["id"] = f"playground:{session_id}:turn:{turn}"

        pipeline, stage = self._resolve_new_lead_stage(organization=organization)
        selected_stage_id = saved.get("stage_id") or stage_id
        if selected_stage_id:
            from apps.crm.models import Stage
            stage = Stage.objects.filter(pk=selected_stage_id, pipeline__organization=organization,
                        pipeline__is_active=True, is_active=True).select_related("pipeline").first()
            if stage is None:
                raise PlaygroundError("Choose an active stage in your organization and restart the test.")
            pipeline = stage.pipeline
        visitor_id = f"playground:{session_id}"
        visitor = _SandboxLead(
            id=visitor_id,
            pk=visitor_id,
            organization=organization,
            organization_id=organization.id,
            pipeline=pipeline,
            pipeline_id=getattr(pipeline, "id", None),
            stage=stage or SimpleNamespace(name="New Lead"),
            stage_id=getattr(stage, "id", None),
            name="Playground Visitor", phone="", email="",
            lead_source=lead_source,
            preview_reminder=deepcopy(saved.get("reminder")),
            shared_document_ids=list(saved.get("sent_files") or []),
            attributes=deepcopy(saved.get("attributes") or {}),
        )
        visitor.attributes[STATE_KEY] = observe_message(
            visitor.attributes.get(STATE_KEY),
            message,
        )

        context_builder = _SandboxContextBuilder(
            organization=organization,
            visitor=visitor,
            conversation=conversation,
            org_info_service=self.org_info_service,
            embedding_service=self.embedding_service,
            retrieval_service=self.retrieval_service,
            pipeline=pipeline,
            stage=stage,
            channel=channel,
        )

        if self.engagement_service is not None:
            service = self.engagement_service
        else:
            from apps.ai_engagement.services.turn_controller import TurnController

            service = TurnController(
                provider=self.provider,
                context_builder=context_builder,
            )
        previous_builder = None
        if self.engagement_service is not None:
            previous_builder = service.context_builder
            service.context_builder = context_builder

        try:
            try:
                from apps.ai_engagement.services.phase5_6_runtime import sandbox_evidence_context
                evidence_scope = (sandbox_evidence_context(organization=organization, lead=visitor,
                                  message=message, provider=self.provider)
                                  if hasattr(organization, "_meta") else nullcontext())
                with evidence_scope:
                    decision = service.engage(organization=organization, lead=visitor)
            except Exception as exc:
                # The production runtime already has validation/schema fail-soft,
                # but Sandbox is synchronous (no Celery retry owner). Never turn
                # a temporary provider/runtime failure into a broken chat surface.
                logger.exception(
                    "AI Sandbox production engagement failed for organization %s; using grounded fallback",
                    getattr(organization, "id", ""),
                )
                decision = self._fallback_decision(
                    organization=organization,
                    visitor=visitor,
                    message=message,
                    cause=exc,
                )
        finally:
            if self.engagement_service is not None:
                service.context_builder = previous_builder

        profile = compile_org_ai_profile_from_context(
            context_builder.organization_context()
        )
        requirements = requirements_for_lead(
            visitor,
            profile.get("qualification", {}).get("requirements", []),
        )
        qualification = state_for_lead(visitor, requirements=requirements)
        try:
            projected = project_answer_updates(
                state=qualification,
                requirements=requirements,
                updates=decision.qualification_updates,
                messages=conversation,
            )
        except ValueError:
            # The public production path should already have rejected unsupported
            # updates. Keep the last validated state if a defensive re-projection
            # still finds anything inconsistent.
            logger.exception(
                "AI Sandbox qualification projection rejected model updates for organization %s",
                getattr(organization, "id", ""),
            )
            projected = qualification

        visitor.attributes = attributes_with_state(visitor, projected)
        qualification = state_for_lead(visitor, requirements=requirements)
        from apps.ai_engagement.services.playground_effects import preview_effects
        sent_files = list(saved.get("sent_files") or [])
        events, files = preview_effects(organization=organization, visitor=visitor, decision=decision,
            requirements=requirements, qualification=qualification, sent_files=sent_files)

        # Previewed stage changes update the same qualification lifecycle used
        # by live CRM stage changes. Persist the contract for the resulting stage.
        qualification = state_for_lead(visitor, requirements=requirements)
        visitor.attributes[STATE_KEY] = contract(
            qualification=qualification,
            requirements=requirements,
            saved=visitor.attributes.get(STATE_KEY),
            organization_id=organization.id,
        )
        if (
            needs_final_composition(decision=decision, events=events, files=files)
            and monotonic() - started_at < 15
        ):
            decision = self._compose_after_preview(
                organization=organization, visitor=visitor, message=message,
                service=service, context_builder=context_builder,
                decision=decision, files=files, events=events,
            )

        # Apply the live welcome helper only to the final customer-facing copy.
        decision = apply_first_inbound_welcome(
            decision=decision,
            organization=organization,
            lead=visitor,
            first_turn=(turn == 1),
        )
        # Record only the question selected for the displayed final response,
        # not an earlier draft which post-effect composition may have replaced.
        if decision.should_engage and decision.next_requirement_id:
            record_last_asked_requirement(
                visitor, decision.next_requirement_id, requirements=requirements,
            )
        visitor.attributes[STATE_KEY] = contract(
            qualification=state_for_lead(visitor, requirements=requirements),
            requirements=requirements,
            saved=visitor.attributes.get(STATE_KEY),
            organization_id=organization.id,
        )
        visitor.attributes[STATE_KEY].update(
            message_id=conversation[-1]["id"],
            processed=True,
            response_hash=response_hash(decision.message),
        )

        response_text = decision.message if decision.should_engage else ""
        updated_history = list(source_history)
        updated_history.extend(
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": response_text},
            ]
        )
        self._save_history(
            organization=organization,
            session_id=session_id,
            history=updated_history,
            attributes=visitor.attributes,
            turn=turn, stage_id=str(visitor.stage_id or ""), sent_files=sent_files,
            channel=channel, lead_source=lead_source,
            reminder=visitor.preview_reminder,
        )

        return PlaygroundResult(
            session_id=session_id,
            message=message,
            response=response_text,
            should_engage=decision.should_engage,
            knowledge=list(context_builder.last_knowledge),
            model=decision.model,
            stage={"id": str(visitor.stage_id or ""), "name": visitor.stage.name,
                   "pipeline": getattr(visitor.pipeline, "name", "")},
            events=events, files=files,
            channel=channel, lead_source=lead_source,
        )

    def _session_context(self, *, saved, channel, lead_source):
        """Keep test routing stable until reset, independently of lead origin."""
        from apps.crm.models import Lead

        valid_sources = dict(Lead._meta.get_field("lead_source").choices)
        requested = {"channel": channel, "lead_source": lead_source}
        allowed = {"channel": {"sandbox", "whatsapp", "instagram"}, "lead_source": valid_sources}
        for key, value in requested.items():
            if value is not None and value not in allowed[key]:
                raise PlaygroundError(f"Invalid {key.replace('_', ' ')} for this test.")
            if value is not None and saved.get(key) and value != saved[key]:
                raise PlaygroundError("Restart chat before changing the test channel or lead source.")

        selected_channel = saved.get("channel") or channel or "sandbox"
        selected_source = saved.get("lead_source") or lead_source or (
            selected_channel if selected_channel in {"whatsapp", "instagram"} else "system"
        )
        return selected_channel, selected_source

    def _compose_after_preview(
        self, *, organization, visitor, message, service, context_builder,
        decision, files, events=None,
    ):
        """Reuse live final composition after this turn's in-memory effects.

        The first validated pass owns every effect. This language-only pass
        sees the resulting state, but cannot change preview state or file choice.
        """
        from apps.ai_engagement.services.file_sharing import FileSharingService
        from apps.ai_engagement.services.phase5_6_runtime import sandbox_evidence_context
        from apps.ai_engagement.services.post_state_finalization_guard import _FINAL_LANGUAGE_ONLY

        if not isinstance(visitor, _SandboxLead) or str(visitor.organization_id) != str(organization.id):
            raise PlaygroundError("Final preview composition requires this organization's Sandbox lead.")
        previous_builder = service.context_builder
        previous_provider = service.provider
        previous_pipeline, previous_stage = context_builder.pipeline, context_builder.stage
        token = _FINAL_LANGUAGE_ONLY.set(True)
        try:
            with preserve_preview_state(visitor):
                context_builder.pipeline = visitor.pipeline
                # A pipeline-less preview uses a name-only visitor stage, not
                # an ORM Stage. Keep the builder on its no-stage path.
                context_builder.stage = visitor.stage if visitor.stage_id else None
                knowledge = deepcopy(context_builder.last_knowledge)
                context = context_builder.build(organization=organization, lead=visitor)
                # Candidate rebuilding must see the recovered evidence, not an
                # empty no-query context. It still cannot choose a different file.
                context = replace(context, knowledge=knowledge)
                selected_ids = {item["id"] for item in files}
                candidates = [
                    item for item in FileSharingService().build_file_candidates(
                        organization=organization, context=context,
                    )
                    if item["document_id"] in selected_ids
                ] if selected_ids else []
                resolved = resolved_preview_actions(
                    visitor=visitor, decision=decision, events=events, files=files,
                    source_message_id=context_builder.conversation[-1]["id"],
                )
                context = replace(
                    context,
                    organization={**context.organization, "_file_candidates": candidates},
                    lead={**context.lead, "operational_state": {
                        **context.lead.get("operational_state", {}),
                        "execution_mode": "sandbox_preview", "resolved_actions": resolved,
                    }},
                )
                service.context_builder = context_builder
                service.provider = previous_provider or OpenAIProvider(timeout_seconds=5)
                evidence_scope = (
                    sandbox_evidence_context(
                        organization=organization, lead=visitor, message=message,
                        provider=service.provider,
                    )
                    if hasattr(organization, "_meta") else nullcontext()
                )
                with evidence_scope:
                    final = service.engage(organization=organization, lead=visitor, context=context)
                return language_only_decision(decision=final, files=files)
        except Exception as exc:
            logger.exception(
                "AI Sandbox post-effect composition failed for organization %s",
                getattr(organization, "id", ""),
            )
            # A failed language pass must not leak its mutations into either the
            # fallback context or the saved session. Fallbacks also cannot act.
            with preserve_preview_state(visitor):
                fallback = self._fallback_decision(
                    organization=organization, visitor=visitor, message=message, cause=exc,
                )
                return language_only_decision(decision=fallback, files=files)
        finally:
            context_builder.pipeline, context_builder.stage = previous_pipeline, previous_stage
            service.context_builder = previous_builder
            service.provider = previous_provider
            _FINAL_LANGUAGE_ONLY.reset(token)

    def _fallback_decision(
        self,
        *,
        organization,
        visitor,
        message: str,
        cause: Exception,
    ) -> EngagementDecision:
        try:
            return build_deterministic_fallback_decision(
                organization=organization,
                lead=visitor,
                latest_inbound=SimpleNamespace(body=message),
            )
        except Exception:
            logger.exception(
                "AI Sandbox deterministic fallback also failed for organization %s",
                getattr(organization, "id", ""),
            )
            # Final response contains no organization-specific claim, so it is
            # safe even when both the provider and backend fallback are impaired.
            return EngagementDecision(
                should_engage=True,
                message=(
                    "I couldn’t retrieve the answer just now. Please try your question again shortly."
                ),
                file_document_id=None,
                crm_actions=[],
                reason="UNKNOWN_INFORMATION",
                reason_code="UNKNOWN_INFORMATION",
                model="sandbox-safe-fallback",
            )

    def _resolve_new_lead_stage(self, *, organization):
        """Use a real active New Lead stage when available, without creating data."""
        try:
            pipelines = organization.pipelines.filter(is_active=True).prefetch_related(
                "stages"
            ).order_by("name")
            for pipeline in pipelines:
                for stage in pipeline.stages.all():
                    normalized = " ".join(str(stage.name or "").casefold().split())
                    if stage.is_active and normalized in {"new lead", "new leads"}:
                        return pipeline, stage
        except Exception:
            logger.exception(
                "AI Sandbox could not resolve a New Lead pipeline for organization %s",
                getattr(organization, "id", ""),
            )
        return None, None

    def reset(self, *, organization, session_id: str) -> None:
        if organization is None:
            raise PlaygroundError("Organization is required.")
        session_id = str(session_id or "").strip()
        if not session_id:
            raise PlaygroundError("session_id is required.")
        try:
            cache.delete(
                self._session_cache_key(
                    organization=organization,
                    session_id=session_id,
                )
            )
        except Exception:
            logger.exception(
                "AI Sandbox cache reset failed for organization %s",
                getattr(organization, "id", ""),
            )

    def _session_cache_key(self, *, organization, session_id: str) -> str:
        return f"shvya:ai:playground-session:{organization.id}:{session_id}"

    def _load_session_payload(self, *, organization, session_id: str) -> dict[str, Any]:
        try:
            payload = cache.get(
                self._session_cache_key(
                    organization=organization,
                    session_id=session_id,
                )
            )
        except Exception:
            logger.exception(
                "AI Sandbox cache read failed for organization %s",
                getattr(organization, "id", ""),
            )
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_history(self, *, organization, session_id: str) -> list[dict[str, str]] | None:
        payload = self._load_session_payload(
            organization=organization,
            session_id=session_id,
        )
        if "history" not in payload:
            return None
        return self._normalize_role_history(payload.get("history"))

    def _save_history(
        self,
        *,
        organization,
        session_id: str,
        history: list[dict[str, Any]],
        attributes: dict | None = None,
        turn: int = 0,
        stage_id: str = "",
        sent_files: list | None = None,
        channel: str = "sandbox",
        lead_source: str = "system",
        reminder: dict | None = None,
    ) -> None:
        normalized = self._normalize_role_history(history)[-self.MAX_HISTORY_MESSAGES :]
        try:
            cache.set(
                self._session_cache_key(
                    organization=organization,
                    session_id=session_id,
                ),
                {
                    "history": normalized,
                    "attributes": deepcopy(attributes or {}),
                    "turn": turn, "stage_id": stage_id, "sent_files": list(sent_files or []),
                    "channel": channel, "lead_source": lead_source,
                    "reminder": deepcopy(reminder),
                },
                timeout=self.SESSION_TTL_SECONDS,
            )
        except Exception:
            # A transient cache outage must not discard a valid AI Sandbox reply.
            logger.exception(
                "AI Sandbox cache write failed for organization %s",
                getattr(organization, "id", ""),
            )

    def _normalize_role_history(
        self,
        history: list[dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for item in (history or [])[-self.MAX_HISTORY_MESSAGES :]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            body = str(item.get("content") or item.get("message") or "").strip()
            if role not in {"user", "assistant"} or not body:
                continue
            normalized.append(
                {
                    "role": role,
                    "content": body[: self.MAX_MESSAGE_LENGTH],
                }
            )
        return normalized

    def _normalize_message(self, message: str) -> str:
        message = (message or "").strip()
        if not message:
            raise PlaygroundError("message is required.")
        return message[: self.MAX_MESSAGE_LENGTH]

    def _normalize_history(
        self,
        *,
        history: list[dict[str, Any]] | None,
        current_message: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        role_history = self._normalize_role_history(history)
        for index, item in enumerate(role_history):
            role = item["role"]
            body = item["content"]
            normalized.append(
                {
                    "id": f"playground:{session_id}:{index + 1}",
                    "direction": "inbound" if role == "user" else "outbound",
                    "speaker": "lead" if role == "user" else "shvya",
                    "body": body,
                    "status": "received" if role == "user" else "sent",
                    "created_at": None,
                }
            )

        normalized.append(
            {
                "id": f"playground:{session_id}:{len(normalized) + 1}",
                "direction": "inbound",
                "speaker": "lead",
                "body": current_message,
                "status": "received",
                "created_at": None,
            }
        )
        return normalized

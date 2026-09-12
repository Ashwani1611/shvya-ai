from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from django.core.cache import cache

from apps.ai_engagement.services.ai_provider import AIProviderError, OpenAIProvider
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.embeddings import EmbeddingError, EmbeddingService
from apps.ai_engagement.services.engagement import (
    ENGAGEMENT_RESPONSE_SCHEMA,
    EngagementError,
    EngagementService,
)
from apps.ai_engagement.services.org_info import OrgInfoService
from apps.ai_engagement.services.retrieval import KnowledgeRetrievalService
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.qualification_state import (
    apply_unambiguous_reply, attributes_with_state, next_requirement,
    project_answer_updates, record_last_asked_requirement, requirements_for_lead, state_for_lead,
)
from apps.ai_engagement.services.runtime_state import STATE_KEY, observe_message, contract, response_hash
from apps.ai_engagement.graph.evidence import check_grounding


class _SandboxLead(SimpleNamespace):
    def _persist_qualification_state(self, attributes):
        self.attributes = deepcopy(attributes)



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

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "message": self.message,
            "response": self.response,
            "should_engage": self.should_engage,
            "knowledge": self.knowledge,
            "model": self.model,
        }


class PlaygroundService:
    """Isolated Chat Playground using the production engagement contract."""

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
        self.engagement_service = engagement_service or EngagementService(provider=provider)
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
    ) -> PlaygroundResult:
        if organization is None:
            raise PlaygroundError("Organization is required.")

        session_id = str(session_id or "").strip()
        if not session_id:
            raise PlaygroundError("session_id is required.")

        message = self._normalize_message(message)
        stored_history = self._load_history(
            organization=organization,
            session_id=session_id,
        )
        source_history = stored_history if stored_history is not None else self._normalize_role_history(history)
        conversation = self._normalize_history(
            history=source_history,
            current_message=message,
            session_id=session_id,
        )
        cached = cache.get(self._session_cache_key(organization=organization, session_id=session_id))
        saved = cached if isinstance(cached, dict) else {}
        turn = int(saved.get("turn", 0)) + 1
        conversation[-1]["id"] = f"playground:{session_id}:turn:{turn}"
        visitor = _SandboxLead(id=f"playground:{session_id}", organization_id=organization.id,
            stage=SimpleNamespace(name="New Lead"), attributes=deepcopy(saved.get("attributes") or {}))
        visitor.attributes[STATE_KEY] = observe_message(visitor.attributes.get(STATE_KEY), message)
        org_info = self.org_info_service.get_or_create(organization=organization)
        knowledge = self._retrieve_knowledge(organization=organization, query=message)

        context = AIContext(
            organization={
                "id": str(organization.id),
                "name": organization.name,
                "ai_enabled": org_info.ai_enabled,
                "about": org_info.about,
                "bot_languages": org_info.bot_languages,
                "qualification_requirements": org_info.qualification_requirements,
                "engagement_instructions": org_info.engagement_instructions,
                "bump_up_enabled": org_info.bump_up_enabled,
                "bump_up_count": org_info.bump_up_count,
            },
            lead={
                "id": f"playground:{session_id}",
                "name": "Playground Visitor",
                "phone": "",
                "email": "",
                "notes": "",
                "attributes": visitor.attributes,
                "lead_source": "playground",
                "stage_entered_at": None,
                "created_at": None,
                "updated_at": None,
            },
            pipeline={},
            stage={"name": "New Lead"},
            contacts=[],
            attributes=[],
            conversation={"message_count": len(conversation), "messages": conversation},
            conversation_summary=None,
            qualification_notes=[],
            knowledge=knowledge,
        )

        profile = compile_org_ai_profile_from_context(context.organization)
        requirements = requirements_for_lead(visitor, profile["qualification"]["requirements"])
        profile["qualification"]["requirements"] = requirements
        direct = apply_unambiguous_reply(lead=visitor, requirements=requirements,
            text=message, source_message_id=conversation[-1]["id"])
        qualification = direct["state"]
        context.lead["attributes"] = visitor.attributes
        try:
            instructions = self.engagement_service._build_instructions(context=context, profile=profile)
            input_text = self.engagement_service._build_input(context=context, profile=profile,
                qualification_state=qualification,
                next_item=next_requirement(requirements, qualification["requirement_states"]))
            provider = self.provider or OpenAIProvider()
            result = self.engagement_service._generate_provider_text(
                provider=provider,
                instructions=instructions,
                input_text=input_text,
                metadata={
                    "organization_id": str(organization.id),
                    "session_id": session_id,
                    "task": "playground",
                    "phase": "primary",
                },
                response_schema=ENGAGEMENT_RESPONSE_SCHEMA,
            )
            try:
                decision = self.engagement_service._normalize_result(result=result)
                self.engagement_service._validate_qualification_decision(
                    decision=decision, context=context,
                    requirements=requirements, qualification_state=qualification,
                )
            except EngagementError as exc:
                decision = self.engagement_service._repair_result_once(
                    provider=provider,
                    organization=organization,
                    lead=SimpleNamespace(id=f"playground:{session_id}"),
                    result=result,
                    original_error=exc,
                    instructions=instructions,
                    input_text=input_text,
                    metadata={
                        "organization_id": str(organization.id),
                        "session_id": session_id,
                        "task": "playground",
                    },
                )
                self.engagement_service._validate_qualification_decision(
                    decision=decision, context=context,
                    requirements=requirements, qualification_state=qualification,
                )
        except AIProviderError as exc:
            raise PlaygroundError("AI Playground generation failed.") from exc
        except EngagementError as exc:
            raise PlaygroundError(str(exc)) from exc

        projected = project_answer_updates(state=qualification, requirements=requirements,
            updates=decision.qualification_updates, messages=conversation)
        grounded = check_grounding({"decision": decision, "context": context,
            "organization": organization, "lead": visitor, "latest_text": message,
            "qualification_state": projected, "requirements": requirements, "runtime_policy": profile})
        decision = grounded.get("decision", decision)
        # Re-project only the surviving validated updates; a rejected model reply
        # must not write answers/actions. Deterministic option answers survive.
        projected = project_answer_updates(state=qualification, requirements=requirements,
            updates=decision.qualification_updates, messages=conversation)
        visitor.attributes = attributes_with_state(visitor, projected)
        if decision.next_requirement_id:
            record_last_asked_requirement(visitor, decision.next_requirement_id, requirements=requirements)
        qualification = state_for_lead(visitor, requirements=requirements)
        visitor.attributes[STATE_KEY] = contract(qualification=qualification, requirements=requirements,
            saved=visitor.attributes.get(STATE_KEY), organization_id=organization.id)
        visitor.attributes[STATE_KEY].update(message_id=conversation[-1]["id"], processed=True,
            response_hash=response_hash(decision.message))
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
            turn=turn,
        )

        return PlaygroundResult(
            session_id=session_id,
            message=message,
            response=response_text,
            should_engage=decision.should_engage,
            knowledge=knowledge,
            model=decision.model,
        )

    def reset(self, *, organization, session_id: str) -> None:
        if organization is None:
            raise PlaygroundError("Organization is required.")
        session_id = str(session_id or "").strip()
        if not session_id:
            raise PlaygroundError("session_id is required.")
        cache.delete(self._session_cache_key(organization=organization, session_id=session_id))

    def _session_cache_key(self, *, organization, session_id: str) -> str:
        return f"shvya:ai:playground-session:{organization.id}:{session_id}"

    def _load_history(self, *, organization, session_id: str) -> list[dict[str, str]] | None:
        payload = cache.get(
            self._session_cache_key(
                organization=organization,
                session_id=session_id,
            )
        )
        if not isinstance(payload, dict) or "history" not in payload:
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
    ) -> None:
        normalized = self._normalize_role_history(history)[-self.MAX_HISTORY_MESSAGES :]
        cache.set(
            self._session_cache_key(
                organization=organization,
                session_id=session_id,
            ),
            {"history": normalized, "attributes": deepcopy(attributes or {}), "turn": turn},
            timeout=self.SESSION_TTL_SECONDS,
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
                    "status": "playground",
                    "created_at": None,
                }
            )

        normalized.append(
            {
                "id": f"playground:{session_id}:{len(normalized) + 1}",
                "direction": "inbound",
                "speaker": "lead",
                "body": current_message,
                "status": "playground",
                "created_at": None,
            }
        )
        return normalized

    def _retrieve_knowledge(self, *, organization, query: str) -> list[dict[str, Any]]:
        try:
            query_vector = self.embedding_service.embed_text(
                query,
                organization_id=organization.id,
                feature="playground_knowledge_query",
                reference_id=str(organization.id),
            )
        except EmbeddingError:
            return []

        results = self.retrieval_service.retrieve_by_vector(
            organization=organization,
            query_vector=query_vector,
            limit=self.MAX_KNOWLEDGE,
        )
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

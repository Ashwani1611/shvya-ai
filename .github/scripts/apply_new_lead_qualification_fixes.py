from pathlib import Path


def replace_once(path, old, new):
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"Expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1))


replace_once(
    "apps/ai_engagement/services/qualification_state.py",
    '''def state_after_stage_change(*, lead, old_stage_name: str, new_stage_name: str) -> dict:
    state = state_for_lead(lead)
    old_name = normalize_stage_name(old_stage_name)
    new_name = normalize_stage_name(new_stage_name)
    if state["qualification_status"] != STATUS_COMPLETED:
        if new_name == QUALIFIED_STAGE:
            state["qualification_status"] = STATUS_COMPLETED
            state["qualification_result"] = RESULT_QUALIFIED
            state["qualification_completed_at"] = timezone.now().isoformat()
            state["current_requirement_id"] = None
            _append_history(state, event="qualification_completed")
        elif old_name == NEW_LEAD_STAGE and new_name != NEW_LEAD_STAGE:
            state["qualification_status"] = STATUS_COMPLETED
            state["qualification_completed_at"] = timezone.now().isoformat()
            state["current_requirement_id"] = None
            _append_history(state, event="qualification_closed_by_stage_change")
    state["qualification_completed"] = state["qualification_status"] == STATUS_COMPLETED
    state["engagement_mode"] = (
        MODE_QUALIFICATION
        if new_name == NEW_LEAD_STAGE and state["qualification_status"] != STATUS_COMPLETED
        else MODE_CONVERSATION
    )
    return state
''',
    '''def state_after_stage_change(*, lead, old_stage_name: str, new_stage_name: str) -> dict:
    """Keep qualification lifecycle separate from ordinary CRM stage movement.

    New Lead is the only stage where qualification may run. Moving an incomplete
    lead elsewhere pauses the questionnaire without destroying its answers. If the
    lead later returns to New Lead, the same unresolved requirement resumes.
    Qualified is the only stage transition that finalizes the backend result.
    """
    state = state_for_lead(lead)
    old_name = normalize_stage_name(old_stage_name)
    new_name = normalize_stage_name(new_stage_name)
    now = timezone.now().isoformat()

    if new_name == QUALIFIED_STAGE:
        changed = (
            state.get("qualification_status") != STATUS_COMPLETED
            or state.get("qualification_result") != RESULT_QUALIFIED
        )
        state["qualification_status"] = STATUS_COMPLETED
        state["qualification_result"] = RESULT_QUALIFIED
        state["qualification_completed_at"] = state.get("qualification_completed_at") or now
        state["current_requirement_id"] = None
        state["next_requirement_id"] = None
        if changed:
            _append_history(state, event="qualification_completed")

    elif old_name == NEW_LEAD_STAGE and new_name != NEW_LEAD_STAGE:
        if state.get("qualification_status") != STATUS_COMPLETED:
            state["engagement_mode"] = MODE_CONVERSATION
            _append_history(state, event="qualification_paused_by_stage_change")

    elif new_name == NEW_LEAD_STAGE and old_name != NEW_LEAD_STAGE:
        history_events = {
            str(item.get("event") or "")
            for item in state.get("history", [])
            if isinstance(item, dict)
        }
        legacy_stage_close = (
            state.get("qualification_status") == STATUS_COMPLETED
            and state.get("qualification_result") != RESULT_QUALIFIED
            and "qualification_closed_by_stage_change" in history_events
            and "qualification_answers_complete" not in history_events
        )
        if legacy_stage_close:
            requirement_states = state.get("requirement_states") or {}
            has_progress = any(
                str(item.get("status") or "")
                in {REQUIREMENT_ASKED, REQUIREMENT_ANSWERED, REQUIREMENT_UNCLEAR}
                for item in requirement_states.values()
                if isinstance(item, dict)
            )
            state["qualification_status"] = (
                STATUS_IN_PROGRESS if has_progress else STATUS_NOT_STARTED
            )
            state["qualification_result"] = ""
            state["qualification_completed_at"] = None
            state["qualification_completed"] = False
            _append_history(state, event="qualification_resumed_from_legacy_stage_close")
        elif state.get("qualification_status") != STATUS_COMPLETED:
            _append_history(state, event="qualification_resumed_by_stage_change")

    state["qualification_completed"] = state["qualification_status"] == STATUS_COMPLETED
    active_requirements = requirements_for_lead(lead)
    return _normalize_runtime_state(state, active_requirements, lead=lead)
''',
)

replace_once(
    "apps/ai_engagement/graph/policy_actions.py",
    "from apps.ai_engagement.services.qualification_state import project_answer_updates\n",
    "from apps.ai_engagement.services.qualification_state import (\n    MODE_QUALIFICATION,\n    project_answer_updates,\n)\n",
)
replace_once(
    "apps/ai_engagement/graph/policy_actions.py",
    '''def _qualification_attribute_updates(*, runtime_policy, qualification_updates, context) -> list[dict[str, Any]]:
    keys = _attribute_keys(context)
    if not keys:
        return []
    criteria = {
        str(item.get("id") or ""): item
        for item in (runtime_policy.get("qualification") or {}).get("criteria") or []
        if isinstance(item, dict)
    }
    updates: list[dict[str, Any]] = []
    for answer in qualification_updates or []:
        criterion = criteria.get(str(answer.get("requirement_id") or "")) or {}
        candidates = [
            str(criterion.get("attribute_key") or "").strip(),
            str(criterion.get("id") or "").strip(),
        ]
        key = next((candidate for candidate in candidates if candidate and candidate in keys), None)
        if key:
            updates.append({"key": key, "value": answer.get("value")})
    return updates
''',
    '''def _qualification_attribute_updates(*, runtime_policy, qualification_updates, context) -> list[dict[str, Any]]:
    definitions = [
        item
        for item in (context.pipeline or {}).get("attribute_definitions") or []
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    ]
    keys = {str(item.get("key") or "").strip() for item in definitions}
    if not keys:
        return []
    criteria = {
        str(item.get("id") or ""): item
        for item in (runtime_policy.get("qualification") or {}).get("criteria") or []
        if isinstance(item, dict)
    }

    def semantic_key(criterion):
        candidates = [
            str(criterion.get("attribute_key") or "").strip(),
            str(criterion.get("id") or "").strip(),
        ]
        exact = next((candidate for candidate in candidates if candidate and candidate in keys), None)
        if exact:
            return exact

        criterion_text = _normalize_text(
            f"{criterion.get('label') or ''} {criterion.get('question') or ''}"
        )
        criterion_tokens = set(re.findall(r"[a-z0-9]+", criterion_text))
        stop = {"a", "an", "and", "are", "do", "does", "for", "how", "is", "of", "or", "the", "to", "what", "where", "which", "your"}
        for definition in definitions:
            name = str(definition.get("name") or "").strip()
            key = str(definition.get("key") or "").strip()
            tokens = set(re.findall(r"[a-z0-9]+", _normalize_text(name or key))) - stop
            if len(tokens) >= 2 and tokens.issubset(criterion_tokens):
                return key
        return None

    updates: list[dict[str, Any]] = []
    for answer in qualification_updates or []:
        criterion = criteria.get(str(answer.get("requirement_id") or "")) or {}
        key = semantic_key(criterion)
        if key:
            updates.append({"key": key, "value": answer.get("value")})
    return updates
''',
)
replace_once(
    "apps/ai_engagement/graph/policy_actions.py",
    '''    messages = (context.conversation or {}).get("messages", [])
    projected = project_answer_updates(
        state=qualification_state,
        requirements=requirements,
        updates=getattr(decision, "qualification_updates", []) or [],
        messages=messages,
    )
''',
    '''    messages = (context.conversation or {}).get("messages", [])
    qualification_active = (
        str(qualification_state.get("engagement_mode") or "").strip().casefold()
        == MODE_QUALIFICATION
    )
    qualification_updates = (
        getattr(decision, "qualification_updates", []) or []
        if qualification_active
        else []
    )
    projected = project_answer_updates(
        state=qualification_state,
        requirements=requirements,
        updates=qualification_updates,
        messages=messages,
    )
''',
)
replace_once(
    "apps/ai_engagement/graph/policy_actions.py",
    '''    qualification_values = {
        _normalize_text(item.get("value"))
        for item in getattr(decision, "qualification_updates", []) or []
    }
''',
    '''    qualification_values = {
        _normalize_text(item.get("value"))
        for item in qualification_updates
    }
''',
)
replace_once(
    "apps/ai_engagement/graph/policy_actions.py",
    '''    deterministic_attrs = _qualification_attribute_updates(
        runtime_policy=runtime_policy,
        qualification_updates=getattr(decision, "qualification_updates", []) or [],
        context=context,
    )
''',
    '''    deterministic_attrs = _qualification_attribute_updates(
        runtime_policy=runtime_policy,
        qualification_updates=qualification_updates,
        context=context,
    )
''',
)
replace_once(
    "apps/ai_engagement/graph/policy_actions.py",
    '''    if evaluation["outcome"] == "qualified":
        qualified_stage_id = qualification_state.get("qualified_stage_id")
''',
    '''    if qualification_active and evaluation["outcome"] == "qualified":
        qualified_stage_id = qualification_state.get("qualified_stage_id")
''',
)

replace_once(
    "apps/ai_engagement/services/engagement.py",
    '''        self._validate_engagement_policy(decision=decision, context=context)
        try:
            projected = project_answer_updates(
''',
    '''        self._validate_engagement_policy(decision=decision, context=context)
        qualifying = qualification_state.get("engagement_mode") == MODE_QUALIFICATION
        if not qualifying:
            if getattr(decision, "qualification_updates", []) or getattr(decision, "next_requirement_id", None):
                raise EngagementError(
                    "Qualification updates/questions are allowed only while the lead is in New Lead qualification mode."
                )
            if str(getattr(decision, "reason_code", "") or "").strip().upper() in {
                "QUALIFICATION_NEXT",
                "QUALIFICATION_CLARIFY",
            }:
                raise EngagementError(
                    "Qualification reason codes are not allowed outside New Lead qualification mode."
                )
        try:
            projected = project_answer_updates(
''',
)
replace_once(
    "apps/ai_engagement/services/engagement.py",
    '''        qualifying = qualification_state.get("engagement_mode") == MODE_QUALIFICATION
        if (next_item and qualifying and decision.should_engage
''',
    '''        if (next_item and qualifying and decision.should_engage
''',
)

replace_once(
    "apps/ai_engagement/services/file_sharing.py",
    "import json\n",
    "import json\nimport re\n",
)
p = Path("apps/ai_engagement/services/file_sharing.py")
text = p.read_text()
start = text.index("    def build_file_candidates(\n")
end = text.index("    # ========================================================\n    # PROVIDER INPUT", start)
new_method = '''    def build_file_candidates(
        self,
        *,
        organization,
        context,
    ) -> list[dict[str, Any]]:
        """Build an organization-scoped allow-list for guided file sharing.

        Normally candidates come from verified RAG chunks. When the latest lead
        message explicitly asks for a file/brochure/catalogue/document, also
        expose configured eligible files so a missing semantic chunk cannot make
        a real uploaded file impossible to send. The response model still must
        match share_instruction and may select only an ID in this allow-list.
        """
        knowledge_items = context.as_dict().get("knowledge", [])
        document_ids = {
            int(item["document_id"])
            for item in knowledge_items
            if item.get("document_id") is not None
        }
        latest_text = ""
        conversation = context.as_dict().get("conversation", {})
        for message in reversed(conversation.get("messages", []) or []):
            if isinstance(message, dict) and message.get("direction") == "inbound":
                latest_text = str(message.get("body") or "").strip()
                if latest_text:
                    break
        explicit_file_request = bool(re.search(
            r"\b(?:brochure|catalog(?:ue)?|pdf|file|document|deck|presentation|menu|prospectus|portfolio|flyer|leaflet|datasheet|price\s*list|pricelist)\b",
            latest_text,
            flags=re.IGNORECASE,
        ))
        if not document_ids and not explicit_file_request:
            return []

        retrieved_documents = (
            self.get_eligible_documents(
                organization=organization,
                document_ids=document_ids,
            )
            if document_ids
            else []
        )
        documents = list(retrieved_documents)
        if explicit_file_request:
            seen = {document.id for document in documents}
            for document in self.get_eligible_documents(organization=organization):
                if document.id not in seen:
                    documents.append(document)
                    seen.add(document.id)
                if len(documents) >= 10:
                    break

        evidence_by_id: dict[int, dict[str, Any]] = {}
        for item in knowledge_items:
            raw_id = item.get("document_id")
            if raw_id is None:
                continue
            document_id = int(raw_id)
            existing = evidence_by_id.get(document_id)
            if existing is None or float(item.get("similarity", 0.0)) > float(existing.get("similarity", 0.0)):
                evidence_by_id[document_id] = item

        candidates = []
        for document in documents[:10]:
            item = evidence_by_id.get(document.id, {})
            candidates.append({
                "document_id": document.id,
                "name": document.name,
                "version": document.version,
                "source_url": document.source_url,
                "share_instruction": document.share_instruction,
                "relevance": float(item.get("similarity", 0.0)),
                "evidence": str(item.get("content") or "").strip(),
            })
        return candidates

'''
p.write_text(text[:start] + new_method + text[end:])

p = Path("apps/ai_engagement/services/qualification.py")
text = p.read_text()
marker = "    # ========================================================\n    # FULL WORKFLOW\n"
if text.count(marker) != 1:
    raise SystemExit("Qualification FULL WORKFLOW marker mismatch")
helper = '''    def append_backend_completion_summary(
        self,
        *,
        organization,
        lead: Lead,
        created_by=None,
    ) -> LeadNote | None:
        """Persist a very short note from backend-verified qualification answers."""
        from apps.ai_engagement.models import OrgInfo
        from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
        from apps.ai_engagement.services.qualification_state import (
            QUALIFIED_STAGE,
            RESULT_QUALIFIED,
            STATUS_COMPLETED,
            normalize_stage_name,
            requirements_for_lead,
            state_for_lead,
        )

        lead.refresh_from_db(fields=["attributes", "stage", "pipeline", "updated_at"])
        if normalize_stage_name(getattr(getattr(lead, "stage", None), "name", "")) != QUALIFIED_STAGE:
            return None
        org_info = OrgInfo.objects.filter(organization=organization).first()
        configured = compile_qualification_requirements(
            org_info.qualification_requirements if org_info else ""
        )["requirements"]
        requirements = requirements_for_lead(lead, configured)
        state = state_for_lead(lead, requirements=requirements)
        if (
            state.get("qualification_status") != STATUS_COMPLETED
            or state.get("qualification_result") != RESULT_QUALIFIED
        ):
            return None

        parts = []
        states = state.get("requirement_states") or {}
        for requirement in requirements:
            requirement_id = str(requirement.get("id") or "")
            answer = states.get(requirement_id) or {}
            if answer.get("status") != "answered":
                continue
            value = answer.get("value")
            if isinstance(value, bool):
                rendered = "Yes" if value else "No"
            else:
                rendered = str(value or "").strip()
            if not rendered:
                continue
            label = compact(str(requirement.get("label") or requirement_id), 64)
            parts.append(f"{label}: {rendered}")
        summary = compact("; ".join(parts), 360)
        if not summary:
            return None
        return self.append_summary(
            lead=lead,
            summary=summary,
            model="deterministic-backend",
            created_by=created_by,
        )

'''
p.write_text(text.replace(marker, helper + marker, 1))

replace_once(
    "apps/ai_engagement/services/crm_executor.py",
    '''        return {
            "type": "pipeline_transition",
            "status": (
                "no_op"
                if (
                    old_pipeline_id == str(stage.pipeline_id)
                    and old_stage_id == str(stage.id)
                )
                else "executed"
            ),
            "pipeline_id": str(stage.pipeline_id),
            "stage_id": str(stage.id),
        }
''',
    '''        qualification_note_id = None
        if str(stage.name or "").strip().casefold() == "qualified":
            from apps.ai_engagement.services.qualification import QualificationService
            note = QualificationService().append_backend_completion_summary(
                organization=organization,
                lead=lead,
                created_by=actor,
            )
            qualification_note_id = str(note.id) if note is not None else None

        return {
            "type": "pipeline_transition",
            "status": (
                "no_op"
                if (
                    old_pipeline_id == str(stage.pipeline_id)
                    and old_stage_id == str(stage.id)
                )
                else "executed"
            ),
            "pipeline_id": str(stage.pipeline_id),
            "stage_id": str(stage.id),
            "qualification_note_id": qualification_note_id,
        }
''',
)

replace_once(
    "apps/ai_engagement/prompts/engagement.py",
    '''8. The application owns completion and stage transition. Model wording cannot
   mark qualification complete.

Rules when engagement_mode is conversation or qualification_status is completed:
''',
    '''8. The application owns completion and stage transition. Model wording cannot
   mark qualification complete.
9. Qualification mode is active only while the lead is currently in the New Lead
   stage. In every other stage, qualification_updates MUST be [] and
   next_requirement_id MUST be null. Never ask a qualification question there.

Rules when engagement_mode is conversation or qualification_status is completed:
''',
)
replace_once(
    "apps/ai_engagement/prompts/engagement.py",
    '''SCHEDULING
- Use supplied working-hour information when available.
''',
    '''AI-GUIDED FILE SHARING
- file_candidates, when present, is the complete organization-owned allow-list.
- If the lead asks for a brochure, catalogue, PDF, document, deck, price list, or
  another configured file and one candidate's share_instruction clearly matches,
  set file_document_id to that candidate's exact ID.
- Never invent a file ID and never claim the file was sent; the backend validates
  and sends the selected file after your response is accepted.

SCHEDULING
- Use supplied working-hour information when available.
''',
)

replace_once(
    "apps/ai_engagement/tests/test_langgraph_policy.py",
    '''        return {
            "qualification_status": "in_progress",
            "requirement_states": {
''',
    '''        return {
            "qualification_status": "in_progress",
            "engagement_mode": "qualification",
            "requirement_states": {
''',
)

test_path = Path("apps/ai_engagement/tests/test_new_lead_orchestration_contract.py")
if test_path.exists():
    raise SystemExit("Regression test file already exists")
test_path.write_text(r'''from __future__ import annotations

from types import SimpleNamespace

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from apps.ai_engagement.graph.policy_actions import build_controlled_actions
from apps.ai_engagement.models import Document, OrgInfo
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.crm_executor import CRMActionExecutor
from apps.ai_engagement.services.file_sharing import FileSharingService
from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
from apps.ai_engagement.services.qualification_state import (
    MODE_CONVERSATION,
    MODE_QUALIFICATION,
    RESULT_QUALIFIED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    apply_unambiguous_reply,
    record_last_asked_requirement,
    state_for_lead,
)
from apps.crm.models import Lead, LeadNote, Pipeline, Stage
from apps.organizations.models import Organization


class NewLeadQualificationLifecycleTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Qualification Scope Org")
        self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales", is_active=True)
        self.new_lead = self.pipeline.stages.get(name="New leads")
        self.qualified = self.pipeline.stages.get(name="Qualified")
        self.follow_up = Stage.objects.create(
            pipeline=self.pipeline,
            name="Follow-up Test",
            display_order=700,
            is_active=True,
        )
        self.org_info = OrgInfo.objects.create(
            organization=self.organization,
            about="Acme manages existing sales leads.",
            bot_languages="English, Hindi",
            qualification_requirements=(
                "What is your budget?\nA. Under 50k\nB. 50k+\n"
                "Which city are you in?\nA. Delhi\nB. Mumbai"
            ),
            engagement_instructions="Be concise and consultative.",
            ai_enabled=True,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.new_lead,
            name="Lead",
            phone="+919999990001",
        )

    def _requirements(self):
        return compile_qualification_requirements(self.org_info.qualification_requirements)["requirements"]

    def test_incomplete_qualification_pauses_outside_new_lead_and_resumes_same_requirement(self):
        requirements = self._requirements()
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="A",
            source_message_id="source-1",
        )
        self.lead.refresh_from_db()
        before = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(before["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(before["engagement_mode"], MODE_QUALIFICATION)
        pending_id = before["current_requirement_id"]

        self.lead.stage = self.follow_up
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()
        paused = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(paused["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(paused["engagement_mode"], MODE_CONVERSATION)
        self.assertEqual(paused["current_requirement_id"], pending_id)

        self.lead.stage = self.new_lead
        self.lead.save(update_fields=["stage", "updated_at"])
        self.lead.refresh_from_db()
        resumed = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(resumed["qualification_status"], STATUS_IN_PROGRESS)
        self.assertEqual(resumed["engagement_mode"], MODE_QUALIFICATION)
        self.assertEqual(resumed["current_requirement_id"], pending_id)

    def test_final_answer_then_qualified_transition_sets_result_and_short_note(self):
        self.org_info.qualification_requirements = "What is your budget?\nA. Under 50k\nB. 50k+"
        self.org_info.save(update_fields=["qualification_requirements", "updated_at"])
        requirements = self._requirements()
        record_last_asked_requirement(self.lead, requirements[0]["id"], requirements=requirements)
        apply_unambiguous_reply(
            lead=self.lead,
            requirements=requirements,
            text="B",
            source_message_id="source-final",
        )
        self.lead.refresh_from_db()
        answered = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(answered["qualification_status"], STATUS_COMPLETED)
        self.assertEqual(answered["engagement_mode"], MODE_CONVERSATION)

        result = CRMActionExecutor().execute(
            organization=self.organization,
            lead=self.lead,
            actions=[{"type": "pipeline_transition", "stage_shift": {"stage_id": str(self.qualified.id)}}],
        )
        self.lead.refresh_from_db()
        final_state = state_for_lead(self.lead, requirements=requirements)
        self.assertEqual(self.lead.stage_id, self.qualified.id)
        self.assertEqual(final_state["qualification_result"], RESULT_QUALIFIED)
        note = LeadNote.objects.get(
            lead=self.lead,
            note_type="system",
            note__startswith="<AI Qualification Summary",
        )
        self.assertIn("50k+", note.note)
        self.assertLessEqual(len(note.note), 500)
        self.assertEqual(result[0]["qualification_note_id"], str(note.id))

    def test_ai_brain_org_fields_are_runtime_source_of_truth(self):
        from apps.ai_engagement.services.context import AIContextBuilder
        context = AIContextBuilder()._build_organization_context(organization=self.organization)
        self.assertEqual(context["about"], self.org_info.about)
        self.assertEqual(context["bot_languages"], self.org_info.bot_languages)
        self.assertEqual(context["engagement_instructions"], self.org_info.engagement_instructions)


class QualificationStagePolicyTests(SimpleTestCase):
    def test_qualified_transition_is_blocked_in_conversation_mode(self):
        context = SimpleNamespace(
            conversation={"messages": [{"id": "m1", "direction": "inbound", "body": "My budget is 75k"}]},
            pipeline={"attribute_definitions": []},
        )
        state = {
            "qualification_status": "in_progress",
            "engagement_mode": MODE_CONVERSATION,
            "requirement_states": {"budget": {"status": "unknown", "value": None, "source_message_id": None}},
            "qualified_stage_id": "qualified-id",
        }
        decision = SimpleNamespace(
            qualification_updates=[{
                "requirement_id": "budget",
                "value": "75k",
                "source_message_id": "m1",
                "evidence": "75k",
            }],
            crm_actions=[],
        )
        actions, result = build_controlled_actions(
            decision=decision,
            context=context,
            runtime_policy={"qualification": {"criteria": [{"id": "budget", "required": True, "pass_condition": None}]}},
            qualification_state=state,
            requirements=[{"id": "budget", "required": True}],
        )
        self.assertFalse(any(item["type"] == "pipeline_transition" for item in actions))
        self.assertEqual(result["evaluation"]["outcome"], "in_progress")

    def test_qualification_answer_maps_to_matching_named_attribute(self):
        context = SimpleNamespace(
            conversation={"messages": [{"id": "m1", "direction": "inbound", "body": "Slow replies"}]},
            pipeline={"attribute_definitions": [{"key": "biggest_challenge", "name": "Biggest challenge", "field_type": "text"}]},
        )
        state = {
            "qualification_status": "in_progress",
            "engagement_mode": MODE_QUALIFICATION,
            "requirement_states": {"challenge": {"status": "unknown", "value": None}},
            "qualified_stage_id": None,
        }
        update = {
            "requirement_id": "challenge",
            "value": "Slow replies",
            "source_message_id": "m1",
            "evidence": "Slow replies",
        }
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[update], crm_actions=[]),
            context=context,
            runtime_policy={"qualification": {"criteria": [{"id": "challenge", "label": "What is your biggest challenge with managing leads", "required": True}]}},
            qualification_state=state,
            requirements=[{"id": "challenge", "required": True}],
        )
        attribute = next(item for item in actions if item["type"] == "attribute_updates")
        self.assertEqual(attribute["updates"], [{"key": "biggest_challenge", "value": "Slow replies"}])

    def test_grounded_reminder_is_kept_without_qualification(self):
        action = {
            "type": "create_reminder",
            "title": "Call lead",
            "description": "Call tomorrow at 6pm",
            "due_at": "2026-09-14T18:00:00+05:30",
        }
        context = SimpleNamespace(
            conversation={"messages": [{"id": "m1", "direction": "inbound", "body": "Please call me tomorrow at 6pm"}]},
            pipeline={"attribute_definitions": []},
        )
        actions, _ = build_controlled_actions(
            decision=SimpleNamespace(qualification_updates=[], crm_actions=[action]),
            context=context,
            runtime_policy={"qualification": {"criteria": []}},
            qualification_state={"engagement_mode": MODE_CONVERSATION, "requirement_states": {}},
            requirements=[],
        )
        self.assertEqual(actions, [action])


class ExplicitGuidedFileCandidateTests(TestCase):
    def test_explicit_file_request_exposes_configured_file_without_rag_chunk(self):
        organization = Organization.objects.create(name="Files Org")
        pipeline = Pipeline.objects.create(organization=organization, name="Sales")
        stage = pipeline.stages.get(name="New leads")
        lead = Lead.objects.create(
            organization=organization,
            pipeline=pipeline,
            stage=stage,
            name="File Lead",
            phone="+919999990099",
        )
        document = Document.objects.create(
            organization=organization,
            name="SHVYA Brochure",
            source_key="shvya-brochure",
            version=1,
            file=SimpleUploadedFile("brochure.pdf", b"brochure", content_type="application/pdf"),
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
            share_instruction="Share this brochure when a lead asks for the brochure or company deck.",
        )
        context = AIContext(
            organization={"id": str(organization.id), "name": organization.name},
            lead={"id": str(lead.id)},
            pipeline={},
            stage={},
            contacts=[],
            attributes=[],
            conversation={
                "message_count": 1,
                "messages": [{"id": "m1", "direction": "inbound", "body": "Please send me your brochure"}],
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=[],
        )
        candidates = FileSharingService().build_file_candidates(organization=organization, context=context)
        self.assertEqual([item["document_id"] for item in candidates], [document.id])
        context.conversation["messages"][0]["body"] = "Thanks"
        self.assertEqual(FileSharingService().build_file_candidates(organization=organization, context=context), [])
''')

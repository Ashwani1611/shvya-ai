"""Apply validated engagement effects to an in-memory Sandbox lead only."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from django.urls import reverse
from django.utils import timezone


def preview_effects(*, organization, visitor, decision, requirements, qualification, sent_files):
    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services.qualification_execution.config import _config, _mapped_value, _mapping_keys
    from apps.ai_engagement.services.qualification_execution.completion import _completion_target
    from apps.ai_engagement.services.qualification_state import (
        attributes_with_state,
        normalize_stage_name,
        state_after_stage_change,
    )
    from apps.crm.models import AttributeDefinition, Stage

    events, files = [], []
    # File eligibility is independent of CRM setup. A new organization can
    # test a guided upload before it has created its first pipeline.
    document_id = decision.file_document_id
    if decision.should_engage and document_id:
        document = Document.objects.filter(pk=document_id, organization=organization, is_active=True,
                    processing_status=Document.ProcessingStatus.COMPLETED).exclude(file='').exclude(share_instruction='').first()
        if document:
            files.append({'id': document.pk, 'name': document.name or Path(document.file.name).name,
                          'url': reverse('ai-playground-file', kwargs={'document_id': document.pk})})
            if document.pk not in sent_files:
                sent_files.append(document.pk)
    if visitor.pipeline_id is None:
        return events, files
    from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
    definitions = {
        item.key: item for item in AttributeDefinition.objects.filter(organization=organization, is_active=True)
        if item.key != "booked_at" and not is_sensitive_attribute_definition({"key": item.key, "name": item.name})
    }
    config = _config(organization=organization, requirements=requirements)
    values = {}
    for action in decision.crm_actions or []:
        if action.get('type') == 'attribute_updates':
            values.update({item['key']: item.get('value') for item in action.get('updates', []) if item.get('key') in definitions})
    for requirement_id, answer in (qualification.get('requirement_states') or {}).items():
        if answer.get('status') != 'answered':
            continue
        for key in _mapping_keys(config, requirement_id):
            if key in definitions:
                values[key] = _mapped_value(config, key, answer.get('value'))
    # Mirror the live attribute service normalization, without calling save().
    values = {key: "" if value is None else str(value).strip() for key, value in values.items()}
    attribute_changes = [
        {"key": key, "name": definitions[key].name, "value": value}
        for key, value in values.items() if visitor.attributes.get(key) != value
    ]
    visitor.attributes.update(deepcopy(values))

    actions = list(decision.crm_actions or [])
    if normalize_stage_name(visitor.stage.name) == "new lead":
        target = _completion_target(lead=visitor, state=qualification, config=config)
        if target and not any(item.get('type') == 'pipeline_transition' for item in actions):
            actions.append({'type': 'pipeline_transition', 'stage_shift': {'stage_id': str(target['id'])}})
    for action in actions:
        if action.get('type') != 'pipeline_transition':
            continue
        stage_id = (action.get('stage_shift') or {}).get('stage_id')
        destination = Stage.objects.filter(pk=stage_id, pipeline__organization=organization,
                                           pipeline__is_active=True, is_active=True).select_related('pipeline').first()
        if destination is None or str(destination.pk) == str(visitor.stage_id):
            continue
        # The shared graph already authorizes routing. Recheck qualification so
        # a preview cannot bypass completion criteria for a custom target stage.
        completion_stage = config.get("completion_stage")
        protected_completion = (
            normalize_stage_name(destination.name) == "qualified"
            or (isinstance(completion_stage, dict) and str(completion_stage.get("id")) == str(destination.pk))
            or str(destination.pk) in config.get("protected_completion_stage_ids", [])
        )
        if protected_completion:
            target = _completion_target(lead=visitor, state=qualification, config=config)
            if not target or str(target['id']) != str(destination.pk):
                continue
        events.append({'type': 'stage_transition', 'from_stage': visitor.stage.name,
                       'stage': destination.name, 'pipeline': destination.pipeline.name, 'status': 'preview'})
        old_stage_name = visitor.stage.name
        visitor.stage, visitor.stage_id = destination, destination.pk
        visitor.pipeline, visitor.pipeline_id = destination.pipeline, destination.pipeline_id
        # Live stage writes invoke this same lifecycle via CRM signals. The
        # preview has no model save/signal, so project it into session state.
        visitor.attributes = attributes_with_state(
            visitor,
            state_after_stage_change(
                lead=visitor,
                old_stage_name=old_stage_name,
                new_stage_name=destination.name,
            ),
        )
        break

    if attribute_changes:
        events.append({"type": "attribute_updates", "status": "preview", "updates": attribute_changes})
    for action in actions:
        if action.get("type") != "create_reminder":
            continue
        from apps.ai_engagement.services.crm_actions import CRMActionSchemaError, validate_crm_actions
        try:
            normalized = validate_crm_actions([action])[0]
            due_at = datetime.fromisoformat(normalized["due_at"])
        except (CRMActionSchemaError, ValueError):
            continue
        if timezone.is_naive(due_at):
            due_at = timezone.make_aware(due_at, timezone.get_current_timezone())
        reminder = {
            "title": normalized["title"],
            "description": normalized["description"],
            "due_at": due_at.isoformat(),
        }
        if reminder != getattr(visitor, "preview_reminder", None):
            visitor.preview_reminder = reminder
            events.append({"type": "reminder", "status": "preview", **reminder})

    return events, files

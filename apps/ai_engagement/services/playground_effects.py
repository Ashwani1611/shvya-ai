"""Apply validated engagement effects to an in-memory Sandbox lead only."""
from copy import deepcopy
from pathlib import Path

from django.urls import reverse


def preview_effects(*, organization, visitor, decision, requirements, qualification, sent_files):
    from apps.ai_engagement.models import Document
    from apps.ai_engagement.services.qualification_execution.config import _config, _mapped_value, _mapping_keys
    from apps.ai_engagement.services.qualification_execution.completion import _completion_target
    from apps.ai_engagement.services.qualification_state import normalize_stage_name
    from apps.crm.models import AttributeDefinition, Stage

    events, files = [], []
    if visitor.pipeline_id is None:
        return events, files
    definitions = {item.key: item for item in AttributeDefinition.objects.filter(organization=organization, is_active=True)}
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
        # a preview can never show an unqualified move to Qualified.
        if destination.name.casefold().strip() == 'qualified':
            target = _completion_target(lead=visitor, state=qualification, config=config)
            if not target or str(target['id']) != str(destination.pk):
                continue
        events.append({'type': 'stage_transition', 'from_stage': visitor.stage.name,
                       'stage': destination.name, 'pipeline': destination.pipeline.name})
        visitor.stage, visitor.stage_id = destination, destination.pk
        visitor.pipeline, visitor.pipeline_id = destination.pipeline, destination.pipeline_id
        break

    document_id = decision.file_document_id
    if decision.should_engage and document_id:
        document = Document.objects.filter(pk=document_id, organization=organization, is_active=True,
                    processing_status=Document.ProcessingStatus.COMPLETED).exclude(file='').exclude(share_instruction='').first()
        if document:
            files.append({'id': document.pk, 'name': document.name or Path(document.file.name).name,
                          'url': reverse('ai-playground-file', kwargs={'document_id': document.pk})})
            if document.pk not in sent_files:
                sent_files.append(document.pk)
    return events, files

"""Save the AI Brain form and queue its knowledge sources after commit."""

from functools import partial

from django.db import transaction

from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.ai_engagement.services.org_info import OrgInfoService
from apps.ai_engagement.tasks import ingest_and_index_document, ingest_and_index_url_source


MAX_URLS_PER_SAVE = 10


def save_ai_brain_configuration(*, organization, data, urls=(), uploaded_file=None):
    """Persist one tenant's configuration without resetting hidden controls.

    URL validation and upload security remain owned by KnowledgeSourceService.
    Validate/create URLs before the file so an invalid URL cannot orphan a file.
    No job can observe the configuration or sources before their transaction commits.
    """
    urls = list(dict.fromkeys(url.strip() for url in urls if url.strip()))
    if len(urls) > MAX_URLS_PER_SAVE:
        raise KnowledgeSourceServiceError(
            f"Add up to {MAX_URLS_PER_SAVE} website URLs at a time."
        )

    source_service = KnowledgeSourceService()
    with transaction.atomic():
        org_info = OrgInfoService().update(organization=organization, data=data)
        for url in urls:
            source = source_service.create_url_source(organization=organization, url=url)
            transaction.on_commit(partial(
                ingest_and_index_url_source.delay,
                source_id=source.id,
                organization_id=organization.id,
            ))
        if uploaded_file is not None:
            _source, document = source_service.create_file_source(
                organization=organization, uploaded_file=uploaded_file,
            )
            transaction.on_commit(partial(
                ingest_and_index_document.delay,
                document_id=document.id,
                organization_id=organization.id,
            ))
    return org_info

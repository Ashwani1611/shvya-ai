import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from celery.exceptions import Retry
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from httpx import Request, Response
from openai import AuthenticationError, RateLimitError
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.ai_engagement.models import Chunk, Document, KnowledgeSource
from apps.ai_engagement.services.credits import AICreditService
from apps.ai_engagement.services.embeddings import EmbeddingService
from apps.ai_engagement.tasks import (
    ingest_and_index_document, ingest_and_index_url_source, reindex_document_embeddings,
)
from apps.ai_engagement.tests.test_knowledge_url_security import FakeConnection, FakeResponse
from apps.ai_engagement.views.document_views import DocumentReindexAPIView
from apps.organizations.models import Organization


class KnowledgeIndexRecoveryTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.media = override_settings(MEDIA_ROOT=self.folder.name)
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.organization = Organization.objects.create(name='Index Recovery')
        AICreditService.add_manual_credits(organization=self.organization, amount=20,
                                         reason='Isolated test credits')
        self.provider = Mock()
        self.provider.embeddings.create.side_effect = self.vectors
        self.transport = patch.object(EmbeddingService, '_get_client', return_value=self.provider)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    @staticmethod
    def vectors(**kwargs):
        return SimpleNamespace(data=[SimpleNamespace(index=index,
            embedding=[0.01] * Chunk.EMBEDDING_DIMENSIONS) for index, _ in enumerate(kwargs['input'])],
            usage=SimpleNamespace(prompt_tokens=20))

    @staticmethod
    def execute(task, *, retries=0, **kwargs):
        # Exercise Celery's real retry construction without publishing to a broker.
        task.push_request(retries=retries, called_directly=False, is_eager=True, args=(), kwargs=kwargs)
        try:
            return task.run(**kwargs)
        finally:
            task.pop_request()

    def document(self, *, version=1, status='pending', active=False, source_key='guide.txt', file=False):
        return Document.objects.create(organization=self.organization, name='Guide',
            source_key=source_key, version=version, processing_status=status, is_active=active,
            file=SimpleUploadedFile('guide.txt', b'Onboarding includes migration training.') if file else '')

    def chunk(self, document):
        return Chunk.objects.create(organization=self.organization, document=document,
                                    content='Onboarding includes migration training.')

    def test_file_transient_retry_reuses_chunks_and_settles_only_successful_provider_call(self):
        old = self.document(status='completed', active=True)
        new = self.document(version=2, file=True)
        self.provider.embeddings.create.side_effect = TimeoutError('temporary timeout')
        with self.assertRaises(Retry) as retry:
            self.execute(ingest_and_index_document, document_id=new.pk, organization_id=self.organization.pk)
        self.assertEqual(retry.exception.when, 30)
        new.refresh_from_db()
        original_chunks = list(new.chunks.values_list('pk', flat=True))
        self.assertTrue(original_chunks)
        self.assertEqual(new.processing_status, 'completed')
        self.assertFalse(new.is_active)
        old.refresh_from_db()
        self.assertTrue(old.is_active)
        wallet = AICreditService.ensure_wallet(self.organization)
        self.assertEqual(wallet.balance, 20)
        self.assertEqual(wallet.reserved_credits, 0)

        self.provider.embeddings.create.side_effect = self.vectors
        result = self.execute(ingest_and_index_document, retries=1, **retry.exception.sig.kwargs)
        self.assertEqual(result['status'], 'completed')
        new.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(list(new.chunks.values_list('pk', flat=True)), original_chunks)
        self.assertTrue(new.is_active)
        self.assertEqual(new.processing_error, '')
        self.assertFalse(old.is_active)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 19)
        self.assertEqual(wallet.reserved_credits, 0)

    def test_positional_task_delivery_retries_without_duplicate_identifier_arguments(self):
        doc = self.document(file=True)
        def transient_once(**kwargs):
            if self.provider.embeddings.create.call_count == 1:
                raise TimeoutError('temporary timeout')
            return self.vectors(**kwargs)
        self.provider.embeddings.create.side_effect = transient_once
        result = ingest_and_index_document.apply(args=(doc.pk, self.organization.pk), throw=False)
        self.assertEqual(result.get(propagate=True)['status'], 'completed')
        self.assertEqual(self.provider.embeddings.create.call_count, 2)
        self.assertEqual(doc.chunks.count(), 1)

    def test_url_transient_retry_is_bound_to_one_document_without_refetch(self):
        source = KnowledgeSource.objects.create(organization=self.organization, name='Guide',
            source_type='url', url='https://93.184.216.34/guide', is_active=True)
        response = FakeResponse(headers={'Content-Type': 'text/html'},
                                chunks=[b'<html><body>Onboarding migration training.</body></html>'])
        self.provider.embeddings.create.side_effect = TimeoutError('temporary timeout')
        with patch('apps.ai_engagement.services.knowledge_url_security._open_pinned_response',
                   return_value=(FakeConnection(), response)) as network:
            with self.assertRaises(Retry) as retry:
                self.execute(ingest_and_index_url_source, source_id=source.pk,
                             organization_id=self.organization.pk)
            original = Document.objects.get(organization=self.organization, source_key=source.url)
            chunks = list(original.chunks.values_list('pk', flat=True))
            self.assertEqual(retry.exception.sig.kwargs['document_id'], original.pk)
            self.provider.embeddings.create.side_effect = self.vectors
            self.execute(ingest_and_index_url_source, retries=1, **retry.exception.sig.kwargs)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(Document.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(list(original.chunks.values_list('pk', flat=True)), chunks)

    def test_exhausted_transient_failure_keeps_previous_version_active(self):
        old = self.document(status='completed', active=True)
        new = self.document(version=2, status='completed')
        self.chunk(new)
        self.provider.embeddings.create.side_effect = TimeoutError('temporary timeout')
        result = self.execute(ingest_and_index_document, retries=3,
                              document_id=new.pk, organization_id=self.organization.pk)
        self.assertEqual(result['reason'], 'embedding_failed')
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertTrue(old.is_active)
        self.assertFalse(new.is_active)
        self.assertEqual(new.processing_status, 'failed')

    def test_authentication_and_exhausted_quota_errors_do_not_retry(self):
        request = Request('POST', 'https://provider.invalid/embeddings')
        cases = [AuthenticationError('denied', response=Response(401, request=request), body={}),
                 RateLimitError('quota', response=Response(429, request=request),
                                body={'code': 'insufficient_quota'})]
        for index, error in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                doc = self.document(source_key=f'permanent-{index}', status='completed')
                self.chunk(doc)
                self.provider.embeddings.create.side_effect = error
                result = self.execute(ingest_and_index_document, document_id=doc.pk,
                                      organization_id=self.organization.pk)
                self.assertEqual(result['status'], 'failed')
                doc.refresh_from_db()
                self.assertEqual(doc.processing_status, 'failed')

    def test_failed_index_can_recover_but_older_recovery_cannot_replace_newer_active_version(self):
        for newer in (False, True):
            with self.subTest(newer=newer):
                key = f'recover-{newer}'
                doc = self.document(source_key=key, status='failed')
                self.chunk(doc)
                current = (self.document(source_key=key, version=2, status='completed', active=True)
                           if newer else None)
                result = self.execute(reindex_document_embeddings, document_id=doc.pk,
                                      organization_id=self.organization.pk)
                self.assertEqual(result['status'], 'completed')
                doc.refresh_from_db()
                self.assertEqual(doc.processing_status, 'completed')
                self.assertEqual(doc.is_active, not newer)
                if current:
                    current.refresh_from_db()
                    self.assertTrue(current.is_active)

    def test_failed_reindex_api_requires_extracted_content_and_current_tenant(self):
        doc = self.document(status='failed')
        actor = SimpleNamespace(is_authenticated=True, organization=self.organization,
                                organization_id=self.organization.pk)
        def request(owner=actor):
            req = APIRequestFactory().post('/reindex/')
            force_authenticate(req, user=owner)
            return DocumentReindexAPIView.as_view()(req, document_id=doc.pk)
        with patch.object(reindex_document_embeddings, 'delay') as queue:
            self.assertEqual(request().status_code, 400)
            queue.assert_not_called()
            self.chunk(doc)
            self.assertEqual(request().status_code, 202)
            queue.assert_called_once_with(document_id=doc.pk, organization_id=self.organization.pk)
            other = Organization.objects.create(name='Other tenant')
            self.assertEqual(request(SimpleNamespace(is_authenticated=True,
                organization=other, organization_id=other.pk)).status_code, 404)

    def test_successful_completed_reindex_clears_temporary_error_without_republishing(self):
        doc = self.document(status='completed', active=False)
        self.chunk(doc)
        doc.processing_error = 'Temporary embedding service failure. Retrying automatically.'
        doc.save(update_fields=['processing_error'])
        result = self.execute(reindex_document_embeddings, document_id=doc.pk,
                              organization_id=self.organization.pk)
        self.assertEqual(result['status'], 'completed')
        doc.refresh_from_db()
        self.assertEqual(doc.processing_error, '')
        self.assertFalse(doc.is_active)

    def test_url_resume_rejects_foreign_document_and_inactive_source(self):
        source = KnowledgeSource.objects.create(organization=self.organization, name='Guide',
            source_type='url', url='https://93.184.216.34/guide', is_active=True)
        foreign = Organization.objects.create(name='Other tenant')
        doc = Document.objects.create(organization=foreign, name='Private',
            source_key=source.url, processing_status='completed')
        result = self.execute(ingest_and_index_url_source, source_id=source.pk,
            organization_id=self.organization.pk, document_id=doc.pk)
        self.assertEqual(result['reason'], 'retry_document_not_found')
        source.is_active = False
        source.save(update_fields=['is_active'])
        result = self.execute(ingest_and_index_url_source, source_id=source.pk,
                              organization_id=self.organization.pk)
        self.assertEqual(result['reason'], 'source_inactive')
        self.provider.embeddings.create.assert_not_called()

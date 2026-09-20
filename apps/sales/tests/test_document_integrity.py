from datetime import date

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from apps.organizations.models import Organization
from apps.sales.models import DocumentType, SalesDocument


class SalesDocumentIntegrityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="SHVYA Sales Test Org")

    def document(self, *, document_type, number, status=SalesDocument.Status.DRAFT):
        return SalesDocument.objects.create(
            organization=self.organization,
            document_type=document_type,
            document_number=number,
            title=f"Test {number}",
            status=status,
            issue_date=date(2026, 9, 21),
        )

    def test_same_formatted_number_can_exist_for_different_document_types(self):
        self.document(document_type=DocumentType.QUOTATION, number="DOC-00001")
        self.document(document_type=DocumentType.INVOICE, number="DOC-00001")

        self.assertEqual(
            SalesDocument.objects.filter(
                organization=self.organization,
                document_number="DOC-00001",
            ).count(),
            2,
        )

    def test_same_number_cannot_repeat_within_one_document_type(self):
        self.document(document_type=DocumentType.QUOTATION, number="QT-00001")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.document(document_type=DocumentType.QUOTATION, number="QT-00001")

    def test_finalized_quotation_response_cannot_be_replayed(self):
        document = self.document(
            document_type=DocumentType.QUOTATION,
            number="QT-00002",
            status=SalesDocument.Status.SENT,
        )
        action_url = reverse(
            "shvya-sales-public-action",
            args=[document.public_token],
        )

        first = self.client.post(
            action_url,
            {
                "action": "accept",
                "name": "Customer One",
                "email": "customer@example.com",
            },
        )
        self.assertEqual(first.status_code, 302)

        document.refresh_from_db()
        accepted_at = document.accepted_at
        self.assertEqual(document.status, SalesDocument.Status.ACCEPTED)
        self.assertEqual(document.accepted_by_name, "Customer One")

        replay = self.client.post(
            action_url,
            {
                "action": "decline",
                "name": "Different Name",
                "email": "different@example.com",
            },
        )
        self.assertEqual(replay.status_code, 302)

        document.refresh_from_db()
        self.assertEqual(document.status, SalesDocument.Status.ACCEPTED)
        self.assertEqual(document.accepted_by_name, "Customer One")
        self.assertEqual(document.accepted_at, accepted_at)

    def test_draft_quotation_cannot_be_accepted_directly(self):
        document = self.document(
            document_type=DocumentType.QUOTATION,
            number="QT-00003",
            status=SalesDocument.Status.DRAFT,
        )
        response = self.client.post(
            reverse("shvya-sales-public-action", args=[document.public_token]),
            {"action": "accept", "name": "Customer"},
        )
        self.assertEqual(response.status_code, 404)

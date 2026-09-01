from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.ai_engagement.models import FAQ
from apps.organizations.models import Organization
from apps.accounts.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(
        name="FAQ API Organization",
    )


@pytest.fixture
def other_organization():
    return Organization.objects.create(
        name="Other FAQ API Organization",
    )


@pytest.fixture
def user(organization):
    return User.objects.create_user(
        email="faq-api@example.com",
        password="test-password-123",
        organization=organization,
    )


@pytest.fixture
def other_user(other_organization):
    return User.objects.create_user(
        email="other-faq-api@example.com",
        password="test-password-123",
        organization=other_organization,
    )


@pytest.fixture
def client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def faq(organization):
    return FAQ.objects.create(
        organization=organization,
        question="What are your business hours?",
        answer="We are open from 9 AM to 6 PM.",
        is_active=True,
    )


class TestFAQListAPI:
    def test_get_returns_organization_faqs(
        self,
        client,
        faq,
        other_organization,
    ):
        FAQ.objects.create(
            organization=other_organization,
            question="Other question",
            answer="Other answer",
        )

        response = client.get(
            "/api/v1/ai-engagement/faqs/"
        )

        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]["id"] == faq.id

    def test_get_active_only(
        self,
        client,
        organization,
        faq,
    ):
        FAQ.objects.create(
            organization=organization,
            question="Inactive question",
            answer="Inactive answer",
            is_active=False,
        )

        response = client.get(
            "/api/v1/ai-engagement/faqs/?active_only=true"
        )

        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]["id"] == faq.id

    def test_post_creates_faq(
        self,
        client,
        organization,
    ):
        response = client.post(
            "/api/v1/ai-engagement/faqs/",
            {
                "question": "What do you offer?",
                "answer": "We provide CRM automation.",
            },
            format="json",
        )

        assert response.status_code == 201

        faq = FAQ.objects.get(
            id=response.data["id"],
        )

        assert faq.organization_id == organization.id
        assert faq.question == "What do you offer?"
        assert faq.answer == (
            "We provide CRM automation."
        )
        assert faq.is_active is True

    def test_post_rejects_empty_question(
        self,
        client,
    ):
        response = client.post(
            "/api/v1/ai-engagement/faqs/",
            {
                "question": "   ",
                "answer": "Valid answer",
            },
            format="json",
        )

        assert response.status_code == 400

    def test_post_rejects_empty_answer(
        self,
        client,
    ):
        response = client.post(
            "/api/v1/ai-engagement/faqs/",
            {
                "question": "Valid question",
                "answer": "   ",
            },
            format="json",
        )

        assert response.status_code == 400

    def test_post_cannot_choose_organization(
        self,
        client,
        organization,
        other_organization,
    ):
        response = client.post(
            "/api/v1/ai-engagement/faqs/",
            {
                "organization": str(
                    other_organization.id
                ),
                "question": "Question",
                "answer": "Answer",
            },
            format="json",
        )

        assert response.status_code == 201

        faq = FAQ.objects.get(
            id=response.data["id"],
        )

        assert faq.organization_id == organization.id


class TestFAQDetailAPI:
    def test_get_faq(
        self,
        client,
        faq,
    ):
        response = client.get(
            f"/api/v1/ai-engagement/faqs/{faq.id}/"
        )

        assert response.status_code == 200
        assert response.data["id"] == faq.id
        assert response.data["question"] == (
            "What are your business hours?"
        )

    def test_patch_faq(
        self,
        client,
        faq,
    ):
        response = client.patch(
            f"/api/v1/ai-engagement/faqs/{faq.id}/",
            {
                "question": "Updated question",
                "answer": "Updated answer",
            },
            format="json",
        )

        assert response.status_code == 200
        assert response.data["question"] == (
            "Updated question"
        )
        assert response.data["answer"] == (
            "Updated answer"
        )

    def test_patch_active_state(
        self,
        client,
        faq,
    ):
        response = client.patch(
            f"/api/v1/ai-engagement/faqs/{faq.id}/",
            {
                "is_active": False,
            },
            format="json",
        )

        assert response.status_code == 200
        assert response.data["is_active"] is False

    def test_delete_faq(
        self,
        client,
        faq,
    ):
        response = client.delete(
            f"/api/v1/ai-engagement/faqs/{faq.id}/"
        )

        assert response.status_code == 204

        assert not FAQ.objects.filter(
            id=faq.id,
        ).exists()

    def test_cross_organization_get_returns_not_found(
        self,
        other_user,
        faq,
    ):
        client = APIClient()
        client.force_authenticate(
            user=other_user,
        )

        response = client.get(
            f"/api/v1/ai-engagement/faqs/{faq.id}/"
        )

        assert response.status_code == 404

    def test_cross_organization_patch_returns_not_found(
        self,
        other_user,
        faq,
    ):
        client = APIClient()
        client.force_authenticate(
            user=other_user,
        )

        response = client.patch(
            f"/api/v1/ai-engagement/faqs/{faq.id}/",
            {
                "answer": "Unauthorized update",
            },
            format="json",
        )

        assert response.status_code == 404

        faq.refresh_from_db()

        assert faq.answer == (
            "We are open from 9 AM to 6 PM."
        )

    def test_cross_organization_delete_returns_not_found(
        self,
        other_user,
        faq,
    ):
        client = APIClient()
        client.force_authenticate(
            user=other_user,
        )

        response = client.delete(
            f"/api/v1/ai-engagement/faqs/{faq.id}/"
        )

        assert response.status_code == 404
        assert FAQ.objects.filter(
            id=faq.id,
        ).exists()


class TestFAQAuthentication:
    def test_list_requires_authentication(self):
        client = APIClient()

        response = client.get(
            "/api/v1/ai-engagement/faqs/"
        )

        assert response.status_code in {
            401,
            403,
        }

    def test_detail_requires_authentication(
        self,
        faq,
    ):
        client = APIClient()

        response = client.get(
            f"/api/v1/ai-engagement/faqs/{faq.id}/"
        )

        assert response.status_code in {
            401,
            403,
        }
from __future__ import annotations

import pytest

from apps.ai_engagement.models import FAQ
from apps.ai_engagement.services.faq import (
    FAQService,
    FAQServiceError,
)
from apps.organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(
        name="FAQ Test Organization",
    )


@pytest.fixture
def other_organization():
    return Organization.objects.create(
        name="Other FAQ Organization",
    )


@pytest.fixture
def service():
    return FAQService()


@pytest.fixture
def faq(organization):
    return FAQ.objects.create(
        organization=organization,
        question="What are your business hours?",
        answer="We are open from 9 AM to 6 PM.",
        is_active=True,
    )


class TestFAQCreate:
    def test_create_faq(
        self,
        service,
        organization,
    ):
        result = service.create(
            organization=organization,
            question="What do you offer?",
            answer="We provide CRM automation.",
        )

        assert result.id is not None
        assert result.organization_id == organization.id
        assert result.question == "What do you offer?"
        assert result.answer == (
            "We provide CRM automation."
        )
        assert result.is_active is True

    def test_create_strips_text(
        self,
        service,
        organization,
    ):
        result = service.create(
            organization=organization,
            question="  What do you offer?  ",
            answer="  CRM automation.  ",
        )

        assert result.question == "What do you offer?"
        assert result.answer == "CRM automation."

    def test_create_rejects_empty_question(
        self,
        service,
        organization,
    ):
        with pytest.raises(
            FAQServiceError,
            match="question cannot be empty",
        ):
            service.create(
                organization=organization,
                question="   ",
                answer="Valid answer",
            )

    def test_create_rejects_empty_answer(
        self,
        service,
        organization,
    ):
        with pytest.raises(
            FAQServiceError,
            match="answer cannot be empty",
        ):
            service.create(
                organization=organization,
                question="Valid question",
                answer="   ",
            )

    def test_create_requires_organization(
        self,
        service,
    ):
        with pytest.raises(
            FAQServiceError,
            match="Organization is required",
        ):
            service.create(
                organization=None,
                question="Question",
                answer="Answer",
            )


class TestFAQList:
    def test_list_returns_only_organization_faqs(
        self,
        service,
        organization,
        other_organization,
        faq,
    ):
        FAQ.objects.create(
            organization=other_organization,
            question="Other question",
            answer="Other answer",
        )

        results = list(
            service.list(
                organization=organization,
            )
        )

        assert len(results) == 1
        assert results[0].id == faq.id

    def test_list_active_only(
        self,
        service,
        organization,
        faq,
    ):
        FAQ.objects.create(
            organization=organization,
            question="Inactive question",
            answer="Inactive answer",
            is_active=False,
        )

        results = list(
            service.list(
                organization=organization,
                active_only=True,
            )
        )

        assert len(results) == 1
        assert results[0].id == faq.id


class TestFAQGet:
    def test_get_returns_organization_faq(
        self,
        service,
        organization,
        faq,
    ):
        result = service.get(
            organization=organization,
            faq_id=faq.id,
        )

        assert result.id == faq.id

    def test_get_cannot_cross_organization_boundary(
        self,
        service,
        other_organization,
        faq,
    ):
        with pytest.raises(
            FAQServiceError,
            match="FAQ not found",
        ):
            service.get(
                organization=other_organization,
                faq_id=faq.id,
            )


class TestFAQUpdate:
    def test_update_question_and_answer(
        self,
        service,
        organization,
        faq,
    ):
        result = service.update(
            organization=organization,
            faq_id=faq.id,
            data={
                "question": "Updated question",
                "answer": "Updated answer",
            },
        )

        assert result.question == "Updated question"
        assert result.answer == "Updated answer"
        assert result.is_active is True

    def test_update_active_state(
        self,
        service,
        organization,
        faq,
    ):
        result = service.update(
            organization=organization,
            faq_id=faq.id,
            data={
                "is_active": False,
            },
        )

        assert result.is_active is False

    def test_update_rejects_unknown_fields(
        self,
        service,
        organization,
        faq,
    ):
        with pytest.raises(
            FAQServiceError,
            match="Unsupported FAQ fields",
        ):
            service.update(
                organization=organization,
                faq_id=faq.id,
                data={
                    "category": "pricing",
                },
            )

    def test_update_rejects_empty_question(
        self,
        service,
        organization,
        faq,
    ):
        with pytest.raises(
            FAQServiceError,
            match="question cannot be empty",
        ):
            service.update(
                organization=organization,
                faq_id=faq.id,
                data={
                    "question": "   ",
                },
            )

    def test_update_rejects_empty_answer(
        self,
        service,
        organization,
        faq,
    ):
        with pytest.raises(
            FAQServiceError,
            match="answer cannot be empty",
        ):
            service.update(
                organization=organization,
                faq_id=faq.id,
                data={
                    "answer": "   ",
                },
            )

    def test_update_cannot_cross_organization_boundary(
        self,
        service,
        other_organization,
        faq,
    ):
        with pytest.raises(
            FAQServiceError,
            match="FAQ not found",
        ):
            service.update(
                organization=other_organization,
                faq_id=faq.id,
                data={
                    "answer": "Malicious update",
                },
            )


class TestFAQActivation:
    def test_activate(
        self,
        service,
        organization,
    ):
        faq = FAQ.objects.create(
            organization=organization,
            question="Question",
            answer="Answer",
            is_active=False,
        )

        result = service.activate(
            organization=organization,
            faq_id=faq.id,
        )

        assert result.is_active is True

    def test_deactivate(
        self,
        service,
        organization,
    ):
        faq = FAQ.objects.create(
            organization=organization,
            question="Question",
            answer="Answer",
            is_active=True,
        )

        result = service.deactivate(
            organization=organization,
            faq_id=faq.id,
        )

        assert result.is_active is False

    def test_activate_is_idempotent(
        self,
        service,
        organization,
        faq,
    ):
        result = service.activate(
            organization=organization,
            faq_id=faq.id,
        )

        assert result.id == faq.id
        assert result.is_active is True

    def test_deactivate_is_idempotent(
        self,
        service,
        organization,
    ):
        faq = FAQ.objects.create(
            organization=organization,
            question="Question",
            answer="Answer",
            is_active=False,
        )

        result = service.deactivate(
            organization=organization,
            faq_id=faq.id,
        )

        assert result.id == faq.id
        assert result.is_active is False


class TestFAQDelete:
    def test_delete(
        self,
        service,
        organization,
        faq,
    ):
        service.delete(
            organization=organization,
            faq_id=faq.id,
        )

        assert not FAQ.objects.filter(
            id=faq.id,
        ).exists()

    def test_delete_cannot_cross_organization_boundary(
        self,
        service,
        other_organization,
        faq,
    ):
        with pytest.raises(
            FAQServiceError,
            match="FAQ not found",
        ):
            service.delete(
                organization=other_organization,
                faq_id=faq.id,
            )

        assert FAQ.objects.filter(
            id=faq.id,
        ).exists()
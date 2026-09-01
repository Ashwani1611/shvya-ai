from __future__ import annotations

from django.core.exceptions import ValidationError

from apps.ai_engagement.models import FAQ


class FAQServiceError(Exception):
    """
    Raised when an FAQ operation cannot be completed safely.
    """


class FAQService:
    """
    Organization-scoped service for managing FAQs.

    Responsibilities:

        - create FAQs
        - retrieve organization FAQs
        - retrieve one organization FAQ
        - update FAQs
        - activate/deactivate FAQs
        - delete FAQs

    The service owns FAQ business logic so API/view layers
    do not manipulate FAQ records directly.
    """

    # ========================================================
    # CREATE
    # ========================================================

    def create(
        self,
        *,
        organization,
        question: str,
        answer: str,
        is_active: bool = True,
    ) -> FAQ:
        """
        Create an organization-owned FAQ.
        """

        if organization is None:
            raise FAQServiceError(
                "Organization is required."
            )

        question = (question or "").strip()
        answer = (answer or "").strip()

        if not question:
            raise FAQServiceError(
                "FAQ question cannot be empty."
            )

        if not answer:
            raise FAQServiceError(
                "FAQ answer cannot be empty."
            )

        if not isinstance(is_active, bool):
            raise FAQServiceError(
                "is_active must be a boolean."
            )

        faq = FAQ(
            organization=organization,
            question=question,
            answer=answer,
            is_active=is_active,
        )

        try:
            faq.full_clean()
            faq.save()
        except ValidationError as exc:
            raise FAQServiceError(
                str(exc)
            ) from exc

        return faq

    # ========================================================
    # LIST
    # ========================================================

    def list(
        self,
        *,
        organization,
        active_only: bool = False,
    ):
        """
        Return FAQs belonging only to the supplied organization.
        """

        if organization is None:
            raise FAQServiceError(
                "Organization is required."
            )

        if not isinstance(active_only, bool):
            raise FAQServiceError(
                "active_only must be a boolean."
            )

        queryset = FAQ.objects.filter(
            organization=organization,
        )

        if active_only:
            queryset = queryset.filter(
                is_active=True,
            )

        return queryset.order_by(
            "-updated_at",
            "-id",
        )

    # ========================================================
    # GET ONE
    # ========================================================

    def get(
        self,
        *,
        organization,
        faq_id,
    ) -> FAQ:
        """
        Retrieve one FAQ belonging to the supplied organization.
        """

        if organization is None:
            raise FAQServiceError(
                "Organization is required."
            )

        if faq_id is None:
            raise FAQServiceError(
                "FAQ ID is required."
            )

        try:
            return FAQ.objects.get(
                id=faq_id,
                organization=organization,
            )
        except FAQ.DoesNotExist as exc:
            raise FAQServiceError(
                "FAQ not found."
            ) from exc

    # ========================================================
    # UPDATE
    # ========================================================

    def update(
        self,
        *,
        organization,
        faq_id,
        data: dict,
    ) -> FAQ:
        """
        Update an FAQ belonging to the supplied organization.

        Supported fields:

            question
            answer
            is_active
        """

        if organization is None:
            raise FAQServiceError(
                "Organization is required."
            )

        if faq_id is None:
            raise FAQServiceError(
                "FAQ ID is required."
            )

        if not isinstance(data, dict):
            raise FAQServiceError(
                "FAQ update data must be a dictionary."
            )

        allowed_fields = {
            "question",
            "answer",
            "is_active",
        }

        unknown_fields = (
            set(data.keys())
            - allowed_fields
        )

        if unknown_fields:
            raise FAQServiceError(
                "Unsupported FAQ fields: "
                + ", ".join(
                    sorted(unknown_fields)
                )
            )

        faq = self.get(
            organization=organization,
            faq_id=faq_id,
        )

        # ----------------------------------------------------
        # QUESTION
        # ----------------------------------------------------

        if "question" in data:
            question = (
                data["question"] or ""
            ).strip()

            if not question:
                raise FAQServiceError(
                    "FAQ question cannot be empty."
                )

            faq.question = question

        # ----------------------------------------------------
        # ANSWER
        # ----------------------------------------------------

        if "answer" in data:
            answer = (
                data["answer"] or ""
            ).strip()

            if not answer:
                raise FAQServiceError(
                    "FAQ answer cannot be empty."
                )

            faq.answer = answer

        # ----------------------------------------------------
        # ACTIVE STATE
        # ----------------------------------------------------

        if "is_active" in data:
            if not isinstance(
                data["is_active"],
                bool,
            ):
                raise FAQServiceError(
                    "is_active must be a boolean."
                )

            faq.is_active = data["is_active"]

        # ----------------------------------------------------
        # MODEL VALIDATION
        # ----------------------------------------------------

        try:
            faq.full_clean(
                validate_unique=False,
            )
        except ValidationError as exc:
            raise FAQServiceError(
                str(exc)
            ) from exc

        faq.save()

        return faq

    # ========================================================
    # ACTIVATE
    # ========================================================

    def activate(
        self,
        *,
        organization,
        faq_id,
    ) -> FAQ:
        """
        Activate an organization-owned FAQ.
        """

        faq = self.get(
            organization=organization,
            faq_id=faq_id,
        )

        if faq.is_active:
            return faq

        faq.is_active = True

        faq.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        return faq

    # ========================================================
    # DEACTIVATE
    # ========================================================

    def deactivate(
        self,
        *,
        organization,
        faq_id,
    ) -> FAQ:
        """
        Deactivate an organization-owned FAQ.

        The FAQ record is retained.
        """

        faq = self.get(
            organization=organization,
            faq_id=faq_id,
        )

        if not faq.is_active:
            return faq

        faq.is_active = False

        faq.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        return faq

    # ========================================================
    # DELETE
    # ========================================================

    def delete(
        self,
        *,
        organization,
        faq_id,
    ) -> None:
        """
        Permanently delete an organization-owned FAQ.
        """

        faq = self.get(
            organization=organization,
            faq_id=faq_id,
        )

        faq.delete()
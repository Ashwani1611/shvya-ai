from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engagement.serializers.faq import FAQSerializer
from apps.ai_engagement.services.faq import (
    FAQService,
    FAQServiceError,
)


class FAQListView(APIView):
    """
    Organization-scoped FAQ collection API.

    GET
        Return FAQs belonging to the authenticated user's
        organization.

    POST
        Create a FAQ for the authenticated user's organization.

    An organization ID is never accepted from the client.
    """

    permission_classes = [
        IsAuthenticated,
    ]

    service_class = FAQService

    @staticmethod
    def get_user_organization(request):
        """
        Resolve the organization from the authenticated user.
        """

        user = request.user

        organization = getattr(
            user,
            "organization",
            None,
        )

        if organization is None:
            raise FAQServiceError(
                "Authenticated user is not associated with an organization."
            )

        return organization

    def get(self, request):
        try:
            organization = self.get_user_organization(
                request
            )

            active_only = request.query_params.get(
                "active_only",
                "false",
            ).lower() == "true"

            faqs = self.service_class().list(
                organization=organization,
                active_only=active_only,
            )

        except FAQServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = FAQSerializer(
            faqs,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        try:
            organization = self.get_user_organization(
                request
            )

            serializer = FAQSerializer(
                data=request.data,
            )

            serializer.is_valid(
                raise_exception=True,
            )

            faq = self.service_class().create(
                organization=organization,
                question=serializer.validated_data[
                    "question"
                ],
                answer=serializer.validated_data[
                    "answer"
                ],
                is_active=serializer.validated_data.get(
                    "is_active",
                    True,
                ),
            )

        except FAQServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = FAQSerializer(
            faq,
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
        )


class FAQDetailView(APIView):
    """
    Organization-scoped single FAQ API.

    GET
        Retrieve one FAQ.

    PATCH
        Update one FAQ.

    DELETE
        Permanently delete one FAQ.
    """

    permission_classes = [
        IsAuthenticated,
    ]

    service_class = FAQService

    @staticmethod
    def get_user_organization(request):
        """
        Resolve the organization from the authenticated user.
        """

        user = request.user

        organization = getattr(
            user,
            "organization",
            None,
        )

        if organization is None:
            raise FAQServiceError(
                "Authenticated user is not associated with an organization."
            )

        return organization

    def get(self, request, faq_id):
        try:
            organization = self.get_user_organization(
                request
            )

            faq = self.service_class().get(
                organization=organization,
                faq_id=faq_id,
            )

        except FAQServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = FAQSerializer(
            faq,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def patch(self, request, faq_id):
        try:
            organization = self.get_user_organization(
                request
            )

            serializer = FAQSerializer(
                data=request.data,
                partial=True,
            )

            serializer.is_valid(
                raise_exception=True,
            )

            faq = self.service_class().update(
                organization=organization,
                faq_id=faq_id,
                data=serializer.validated_data,
            )

        except FAQServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        response_serializer = FAQSerializer(
            faq,
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )

    def delete(self, request, faq_id):
        try:
            organization = self.get_user_organization(
                request
            )

            self.service_class().delete(
                organization=organization,
                faq_id=faq_id,
            )

        except FAQServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            status=status.HTTP_204_NO_CONTENT,
        )
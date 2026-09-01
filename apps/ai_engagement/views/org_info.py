from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engagement.serializers.org_info import (
    OrgInfoSerializer,
)
from apps.ai_engagement.services.org_info import (
    OrgInfoService,
    OrgInfoServiceError,
)


class OrgInfoView(
    APIView
):
    """
    Organization AI configuration API.

    GET
        Return the authenticated user's organization's AI config.

    PATCH
        Update the authenticated user's organization's AI config.

    Organization ownership always comes from the authenticated
    user. The client cannot provide another organization ID.
    """

    permission_classes = [
        IsAuthenticated,
    ]

    service_class = OrgInfoService

    # ========================================================
    # ORGANIZATION
    # ========================================================

    @staticmethod
    def get_user_organization(
        request,
    ):
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

            raise OrgInfoServiceError(
                "Authenticated user is not associated with an organization."
            )

        return organization

    # ========================================================
    # GET
    # ========================================================

    def get(
        self,
        request,
    ):

        try:

            organization = (
                self.get_user_organization(
                    request,
                )
            )

            org_info = (
                self.service_class().get_or_create(
                    organization=organization,
                )
            )

        except OrgInfoServiceError as exc:

            return Response(
                {
                    "detail": str(
                        exc
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = OrgInfoSerializer(
            org_info,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    # ========================================================
    # PATCH
    # ========================================================

    def patch(
        self,
        request,
    ):

        organization = None

        try:

            organization = (
                self.get_user_organization(
                    request,
                )
            )

            serializer = OrgInfoSerializer(
                data=request.data,
                partial=True,
            )

            serializer.is_valid(
                raise_exception=True,
            )

            org_info = (
                self.service_class().update(
                    organization=organization,
                    data=serializer.validated_data,
                )
            )

        except OrgInfoServiceError as exc:

            return Response(
                {
                    "detail": str(
                        exc
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = (
            OrgInfoSerializer(
                org_info,
            )
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )
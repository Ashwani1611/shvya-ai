from rest_framework import status
from rest_framework.permissions import (
    IsAuthenticated,
)
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engagement.serializers.playground import (
    PlaygroundRequestSerializer,
    PlaygroundResponseSerializer,
)
from apps.ai_engagement.services.playground import (
    PlaygroundError,
    PlaygroundService,
)


class PlaygroundAPIView(APIView):
    """
    Organization-scoped Chat Playground.

    The organization always comes from the authenticated user.

    The client cannot select another organization.

    This endpoint:
        - does not resolve a Lead
        - does not mutate CRM
        - does not create LeadNotes
        - does not send WhatsApp
        - does not call Meta
    """

    permission_classes = [
        IsAuthenticated,
    ]

    def post(
        self,
        request,
        *args,
        **kwargs,
    ):
        user = request.user

        organization = getattr(
            user,
            "organization",
            None,
        )

        if organization is None:
            return Response(
                {
                    "error": (
                        "Authenticated user is not associated "
                        "with an organization."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PlaygroundRequestSerializer(
            data=request.data,
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:
            result = (
                PlaygroundService().run(
                    organization=organization,
                    session_id=(
                        serializer.validated_data[
                            "session_id"
                        ]
                    ),
                    message=(
                        serializer.validated_data[
                            "message"
                        ]
                    ),
                    history=(
                        serializer.validated_data.get(
                            "history",
                            [],
                        )
                    ),
                )
            )

        except PlaygroundError as exc:
            return Response(
                {
                    "error": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = (
            PlaygroundResponseSerializer(
                result.as_dict()
            )
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )
from rest_framework import status
from rest_framework.authentication import (
    BaseAuthentication,
    SessionAuthentication,
)
from rest_framework.permissions import (
    IsAuthenticated,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import (
    JWTAuthentication,
)

from apps.ai_engagement.serializers.playground import (
    PlaygroundRequestSerializer,
    PlaygroundResetSerializer,
    PlaygroundResponseSerializer,
)
from apps.ai_engagement.services.playground import (
    PlaygroundError,
    PlaygroundService,
)
from apps.crm.authentication import (
    get_crm_authenticated_user,
)


class CRMPlaygroundSessionAuthentication(
    BaseAuthentication
):
    """
    Authenticate the dedicated SHVYA CRM dashboard session.

    The dashboard intentionally uses ``shvya_crm_sessionid`` instead of
    Django's default ``sessionid`` cookie. The playground endpoint lives
    under ``/api/``, so normal dashboard middleware does not attach that
    CRM identity to the API request. Resolve the dedicated CRM session
    explicitly here while still enforcing CSRF for cookie authentication.
    """

    def authenticate(self, request):
        user = get_crm_authenticated_user(
            request._request
        )

        if user is None or not user.is_active:
            return None

        SessionAuthentication().enforce_csrf(
            request
        )

        return user, None


class PlaygroundAPIView(APIView):
    """Organization-scoped Chat Playground with an explicit reset contract."""

    authentication_classes = [
        CRMPlaygroundSessionAuthentication,
        JWTAuthentication,
    ]

    permission_classes = [
        IsAuthenticated,
    ]

    def _organization(self, request):
        return getattr(request.user, "organization", None)

    def post(self, request, *args, **kwargs):
        organization = self._organization(request)

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

        serializer = PlaygroundRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = PlaygroundService().run(
                organization=organization,
                session_id=serializer.validated_data["session_id"],
                message=serializer.validated_data["message"],
                history=serializer.validated_data.get("history", []),
                **({"stage_id": str(serializer.validated_data["stage_id"])} if serializer.validated_data.get("stage_id") else {}),
            )
        except PlaygroundError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_serializer = PlaygroundResponseSerializer(result.as_dict())
        return Response(
            response_serializer.data,
            status=status.HTTP_200_OK,
        )

    def delete(self, request, *args, **kwargs):
        organization = self._organization(request)
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

        serializer = PlaygroundResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session_id = serializer.validated_data["session_id"]

        try:
            PlaygroundService().reset(
                organization=organization,
                session_id=session_id,
            )
        except PlaygroundError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "session_id": session_id,
                "reset": True,
            },
            status=status.HTTP_200_OK,
        )


class PlaygroundFileAPIView(PlaygroundAPIView):
    """Authenticated tenant-scoped download for a guided-file preview."""
    http_method_names = ["get", "head", "options"]

    def get(self, request, document_id):
        from pathlib import Path
        from django.http import FileResponse, Http404
        from apps.ai_engagement.models import Document
        organization = self._organization(request)
        if organization is None:
            raise Http404
        document = Document.objects.filter(pk=document_id, organization=organization, is_active=True,
                     processing_status=Document.ProcessingStatus.COMPLETED).exclude(file="").exclude(share_instruction="").first()
        if document is None:
            raise Http404
        try:
            response = FileResponse(document.file.open("rb"), as_attachment=True,
                                    filename=Path(document.file.name).name)
        except (FileNotFoundError, OSError):
            raise Http404 from None
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response

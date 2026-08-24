import logging
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.crm.authentication import SHVYAAPIKeyAuthentication
from apps.accounts.models import User
from apps.crm.models import Lead, Pipeline, Stage, LeadNote
from apps.crm.serializers import LeadUpsertSerializer
from services.crm.lead_service import DuplicateLeadError, upsert_lead

logger = logging.getLogger(__name__)

class LeadUpsertAPIView(APIView):

    authentication_classes = [
        SHVYAAPIKeyAuthentication
    ]

    permission_classes = [
        IsAuthenticated
    ]

    def post(
        self,
        request,
        *args,
        **kwargs,
    ):

        api_key = request.auth

        if not api_key.can_upsert_leads:

            return Response(
                {
                    "message": (
                        "This API key is not authorized "
                        "to upsert leads."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = LeadUpsertSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        data = serializer.validated_data

        organization = api_key.organization

        pipeline_name = data.pop(
            "pipeline",
            None,
        )

        stage_name = data.pop(
            "stage",
            None,
        )

        pipeline = None
        stage = None

        if pipeline_name:

            pipeline = get_object_or_404(
                Pipeline,
                organization=organization,
                name=pipeline_name,
                is_active=True,
            )

        if stage_name:

            if pipeline is None:

                return Response(
                    {
                        "message": (
                            "Pipeline is required "
                            "when stage is supplied."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            stage = get_object_or_404(
                Stage,
                pipeline=pipeline,
                name=stage_name,
                is_active=True,
            )

        try:

            lead, created = upsert_lead(
                organization=organization,
                pipeline=pipeline,
                stage=stage,
                name=data.get("name"),
                phone=data.get("phone"),
                email=data.get(
                    "email",
                    "",
                ),
                notes=data.get(
                    "notes",
                    "",
                ),
                attributes=data.get(
                    "attributes",
                    {},
                ),
            )

        except DuplicateLeadError as exc:

            return Response(
                {
                    "message": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except DjangoValidationError as exc:

            return Response(
                {
                    "message": (
                        exc.message_dict
                        if hasattr(
                            exc,
                            "message_dict",
                        )
                        else exc.messages
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        except Exception:

            logger.exception(
                "SHVYA Lead Upsert API failed."
            )

            return Response(
                {
                    "message": (
                        "Internal server error."
                    )
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "lead_id": str(
                    lead.id
                ),
                "message": (
                    "Lead added successfully."
                    if created
                    else "Lead updated successfully."
                ),
            },
            status=status.HTTP_200_OK,
        )


class LeadListAPIView(ListAPIView):

    authentication_classes = [
        SHVYAAPIKeyAuthentication
    ]

    permission_classes = [
        IsAuthenticated
    ]

    def get_queryset(self):

        organization = (
            self.request.auth.organization
        )

        qs = Lead.objects.filter(
            organization=organization
        ).select_related(
            "pipeline",
            "stage",
        )

        search = (
            self.request.query_params.get(
                "search"
            )
        )

        if search:

            qs = qs.filter(
                Q(
                    name__icontains=search
                )
                | Q(
                    phone__icontains=search
                )
                | Q(
                    email__icontains=search
                )
            )

        pipeline_name = (
            self.request.query_params.get(
                "pipeline"
            )
        )

        if pipeline_name:

            qs = qs.filter(
                pipeline__name=pipeline_name
            )

        stage_name = (
            self.request.query_params.get(
                "stage"
            )
        )

        if stage_name:

            qs = qs.filter(
                stage__name=stage_name
            )

        tag_name = (
            self.request.query_params.get(
                "tag"
            )
        )

        if tag_name:

            qs = qs.filter(
                tags__tag__name=tag_name
            )

        return qs.distinct()

    def list(
        self,
        request,
        *args,
        **kwargs,
    ):

        qs = self.get_queryset()

        data = [
            {
                "id": str(
                    lead.id
                ),
                "name": lead.name,
                "phone": lead.phone,
                "email": lead.email,
                "pipeline": (
                    lead.pipeline.name
                ),
                "stage": (
                    lead.stage.name
                ),
                "created_at": (
                    lead.created_at.isoformat()
                ),
            }
            for lead in qs[:100]
        ]

        return Response(
            {
                "count": qs.count(),
                "results": data,
            }
        )


class BulkMoveStageAPIView(APIView):

    authentication_classes = [
        SHVYAAPIKeyAuthentication
    ]

    permission_classes = [
        IsAuthenticated
    ]

    def post(
        self,
        request,
        *args,
        **kwargs,
    ):

        api_key = request.auth

        if not api_key.can_upsert_leads:

            return Response(
                {
                    "message": (
                        "This API key is not authorized "
                        "to update leads."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        lead_ids = request.data.get(
            "lead_ids",
            [],
        )

        stage_name = request.data.get(
            "stage"
        )

        if not lead_ids:

            return Response(
                {
                    "message": (
                        "lead_ids is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not stage_name:

            return Response(
                {
                    "message": (
                        "stage is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization = (
            api_key.organization
        )

        stage = get_object_or_404(
            Stage,
            pipeline__organization=organization,
            name=stage_name,
            is_active=True,
        )

        leads = Lead.objects.filter(
            organization=organization,
            id__in=lead_ids,
        )

        updated_count = leads.update(
            stage=stage
        )

        return Response(
            {
                "updated": updated_count,
                "stage": stage.name,
            },
            status=status.HTTP_200_OK,
        )


# ============================================================
# CRM PIPELINE ACCESS
# ============================================================

def get_user_pipelines(user):
    """
    Return the active pipelines this CRM user is allowed to access.

    Access rules:

    ADMIN
        Can access every active pipeline belonging to
        their organization.

    AGENT
        Can access only the active pipeline they own.

    SUPERADMIN
        Does not use the organization CRM dashboard.

    Any unsupported role
        Gets no pipeline access.
    """

    if not user or not user.organization_id:
        return Pipeline.objects.none()

    if user.role == User.Role.ADMIN:

        return Pipeline.objects.filter(
            organization_id=user.organization_id,
            is_active=True,
        )

    if user.role == User.Role.AGENT:

        return Pipeline.objects.filter(
            organization_id=user.organization_id,
            owner=user,
            is_active=True,
        )

    return Pipeline.objects.none()


# ============================================================
# PHASE 3 — DASHBOARD / WEB VIEWS
# ============================================================


STAGE_THEMES = [
    {
        "hex": "#f97316",
        "avatar_bg": "bg-orange-100",
        "avatar_text": "text-orange-700",
    },
    {
        "hex": "#3b82f6",
        "avatar_bg": "bg-blue-100",
        "avatar_text": "text-blue-700",
    },
    {
        "hex": "#a855f7",
        "avatar_bg": "bg-purple-100",
        "avatar_text": "text-purple-700",
    },
    {
        "hex": "#22c55e",
        "avatar_bg": "bg-green-100",
        "avatar_text": "text-green-700",
    },
    {
        "hex": "#ef4444",
        "avatar_bg": "bg-red-100",
        "avatar_text": "text-red-700",
    },
    {
        "hex": "#8b5cf6",
        "avatar_bg": "bg-violet-100",
        "avatar_text": "text-violet-700",
    },
    {
        "hex": "#14b8a6",
        "avatar_bg": "bg-teal-100",
        "avatar_text": "text-teal-700",
    },
    {
        "hex": "#6366f1",
        "avatar_bg": "bg-indigo-100",
        "avatar_text": "text-indigo-700",
    },
    {
        "hex": "#f59e0b",
        "avatar_bg": "bg-amber-100",
        "avatar_text": "text-amber-700",
    },
]



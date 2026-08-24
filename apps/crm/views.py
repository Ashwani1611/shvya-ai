import logging

from django.contrib.auth import authenticate
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.crm.authentication import SHVYAAPIKeyAuthentication
from apps.crm.models import Lead, Pipeline, Stage, LeadNote
from apps.crm.serializers import LeadUpsertSerializer

from apps.accounts.models import User

from apps.accounts.session_utils import (
    get_session_store,
    save_session_cookie,
    set_authenticated_user,
)

from services.lead_service import DuplicateLeadError, upsert_lead


logger = logging.getLogger(__name__)


# ============================================================
# CRM SESSION HELPERS
# ============================================================

CRM_SESSION_AREA = "dashboard"


def get_crm_session(request):
    """
    Return the dedicated SHVYA CRM session.

    The CRM uses its own cookie:

        shvya_crm_sessionid

    This keeps the CRM session independent from:

        - Django Admin
        - Superadmin
        - Other SHVYA areas
    """

    return get_session_store(
        request,
        CRM_SESSION_AREA,
    )


def crm_session_is_authenticated(request):
    """
    Determine whether the dedicated CRM session contains
    Django authentication state.

    IMPORTANT:

    Do NOT use request.user.is_authenticated here.

    request.user belongs to Django's normal authentication
    middleware/session and can remain authenticated even after
    the dedicated CRM session has been cleared.
    """

    session = get_crm_session(request)

    return bool(
        session.get("_auth_user_id")
        and session.get("_auth_user_backend")
        and session.get("_auth_user_hash")
    )


def get_crm_authenticated_user(request):
    """
    Resolve the user stored inside the dedicated CRM session.

    Returns:

        User instance

    or:

        None
    """

    session = get_crm_session(request)

    user_id = session.get("_auth_user_id")
    backend_path = session.get("_auth_user_backend")

    if not user_id or not backend_path:
        return None

    try:
        from django.contrib.auth import load_backend

        backend = load_backend(backend_path)

        user = backend.get_user(user_id)

        if user is None:
            return None

        # ----------------------------------------------------
        # Verify session auth hash.
        # ----------------------------------------------------

        session_hash = session.get("_auth_user_hash")

        if session_hash:
            if not user.get_session_auth_hash() == session_hash:
                return None

        return user

    except Exception:
        logger.exception(
            "Unable to resolve CRM authenticated user."
        )

        return None


def crm_login_required(view_func):
    """
    Dedicated CRM authentication decorator.

    This is intentionally separate from Django's standard
    @login_required because SHVYA CRM uses its own session
    cookie.

    If the CRM session is missing, redirect to:

        /dashboard/login/
    """

    def wrapped_view(request, *args, **kwargs):

        user = get_crm_authenticated_user(request)

        if user is None:

            return redirect(
                "crm-login"
            )

        # ----------------------------------------------------
        # Make the CRM user available to the view.
        #
        # This avoids depending on Django's global session.
        # ----------------------------------------------------

        request.crm_user = user

        return view_func(
            request,
            *args,
            **kwargs,
        )

    wrapped_view.__name__ = view_func.__name__
    wrapped_view.__doc__ = view_func.__doc__
    wrapped_view.__module__ = view_func.__module__

    return wrapped_view


# ============================================================
# CRM LOGIN
# ============================================================


def crm_login_view(request):
    """
    Dedicated SHVYA CRM login page.

    CRM authentication is stored inside the dedicated
    SHVYA CRM session:

        shvya_crm_sessionid

    This keeps CRM authentication independent from:

        - Django Admin
        - Superadmin
        - other SHVYA sessions
    """

    # ========================================================
    # ALREADY AUTHENTICATED
    # ========================================================

    crm_user = get_crm_authenticated_user(request)

    if crm_user is not None:

        return redirect(
            "crm-dashboard"
        )

    # ========================================================
    # LOGIN SUBMISSION
    # ========================================================

    if request.method == "POST":

        email = request.POST.get(
            "email",
            "",
        ).strip()

        password = request.POST.get(
            "password",
            "",
        )

        # ----------------------------------------------------
        # Basic validation
        # ----------------------------------------------------

        if not email:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Please enter your email address."
                    ),
                    "email": email,
                },
            )

        if not password:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Please enter your password."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # Authenticate using Django's authentication backend.
        #
        # User.USERNAME_FIELD = "email"
        # ----------------------------------------------------

        user = authenticate(
            request=request,
            username=email,
            password=password,
        )

        # ----------------------------------------------------
        # Invalid credentials
        # ----------------------------------------------------

        if user is None:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Invalid email or password."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # User disabled
        # ----------------------------------------------------

        if not user.is_active:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Your account is currently disabled. "
                        "Please contact your administrator."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # Organization validation
        # ----------------------------------------------------

        organization = user.organization

        if organization is None:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Your account is not associated "
                        "with an organization."
                    ),
                    "email": email,
                },
            )

        # ----------------------------------------------------
        # Organization disabled
        # ----------------------------------------------------

        if not organization.is_active:

            return render(
                request,
                "crm/login.html",
                {
                    "login_error": (
                        "Your organization account is "
                        "currently disabled. "
                        "Please contact your administrator."
                    ),
                    "email": email,
                },
            )

        # ====================================================
        # LOAD DEDICATED CRM SESSION
        # ====================================================

        crm_session = get_crm_session(
            request
        )

        # ====================================================
        # AUTHENTICATE USER INTO CRM SESSION
        # ====================================================

        set_authenticated_user(
            crm_session,
            user,
        )

        # ====================================================
        # REDIRECT TO DASHBOARD
        # ====================================================

        response = redirect(
            "crm-dashboard"
        )

        # ====================================================
        # SAVE DEDICATED CRM COOKIE
        # ====================================================

        save_session_cookie(
            request,
            response,
            crm_session,
            CRM_SESSION_AREA,
        )

        # ====================================================
        # PREVENT LOGIN PAGE CACHING
        # ====================================================

        response["Cache-Control"] = (
            "no-cache, no-store, must-revalidate"
        )

        response["Pragma"] = "no-cache"
        response["Expires"] = "0"

        return response

    # ========================================================
    # GET — SHOW LOGIN PAGE
    # ========================================================

    response = render(
        request,
        "crm/login.html",
        {
            "email": "",
        },
    )

    response["Cache-Control"] = (
        "no-cache, no-store, must-revalidate"
    )

    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


    # ============================================================
# CRM PROFILE
# ============================================================


@crm_login_required
def crm_profile_view(request):
    """
    Display the profile of the currently authenticated CRM user.

    CRM authentication is resolved through the dedicated
    SHVYA CRM session and exposed as:

        request.crm_user

    This intentionally does not rely on request.user because
    SHVYA CRM authentication is isolated from other SHVYA areas.
    """

    user = request.crm_user

    organization = user.organization

    return render(
        request,
        "crm/profile.html",
        {
            "profile_user": user,
            "organization": organization,
        },
    )


# ============================================================
# PHASE 2 — API VIEWS
# ============================================================
#
# NOTE: CRM logout lives in apps.accounts.views.crm_logout_view
# (routed as "crm-logout" in apps.accounts.urls). It flushes the
# entire session rather than clearing individual keys, which is
# the stronger/correct approach — an earlier, weaker duplicate of
# this view previously lived here unused and has been removed.


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


@crm_login_required
def dashboard_view(request):

    user = request.crm_user

    pipelines = get_user_pipelines(
        user
    )

    requested_pipeline_id = request.GET.get(
        "pipeline"
    )

    selected_pipeline = None

    # --------------------------------------------------------
    # ADMIN
    #
    # Admins can access every active pipeline in their
    # organization.
    #
    # Default pipeline is always "Leads" when it exists.
    # --------------------------------------------------------

    if user.role == User.Role.ADMIN:

        if requested_pipeline_id:

            selected_pipeline = (
                pipelines
                .filter(
                    id=requested_pipeline_id,
                )
                .first()
            )

        if selected_pipeline is None:

            selected_pipeline = (
                pipelines
                .filter(
                    name="Leads",
                )
                .first()
            )

        if selected_pipeline is None:

            selected_pipeline = (
                pipelines.first()
            )

    # --------------------------------------------------------
    # AGENT
    #
    # Agents are restricted to their assigned/owned
    # pipeline.
    #
    # A requested pipeline ID is ignored if it is not the
    # Agent's permitted pipeline.
    # --------------------------------------------------------

    elif user.role == User.Role.AGENT:

        selected_pipeline = (
            pipelines.first()
        )

    selected_pipeline_id = (
        selected_pipeline.id
        if selected_pipeline
        else None
    )

    response = render(
        request,
        "crm/dashboard.html",
        {
            "pipelines": pipelines,
            "selected_pipeline_id": (
                str(
                    selected_pipeline_id
                )
                if selected_pipeline_id
                else None
            ),
            "crm_user": user,
        },
    )

    response["Cache-Control"] = (
        "no-cache, no-store, must-revalidate"
    )

    response["Pragma"] = "no-cache"
    response["Expires"] = "0"

    return response


@crm_login_required
@require_GET
def lead_table_partial(request):

    user = request.crm_user

    organization = user.organization

    pipeline_id = request.GET.get(
        "pipeline"
    )

    search = request.GET.get(
        "search",
        "",
    ).strip()

    if (
        not pipeline_id
        or pipeline_id == "None"
    ):

        return render(
            request,
            "crm/partials/lead_table.html",
            {
                "stage_groups": [],
                "all_stages": [],
            },
        )

    pipeline = get_user_pipelines(
         user
        ).filter(
        id=pipeline_id,
    ).first()

    if not pipeline:

        return render(
            request,
            "crm/partials/lead_table.html",
            {
                "stage_groups": [],
                "all_stages": [],
            },
        )

    stages = Stage.objects.filter(
        pipeline=pipeline,
        is_active=True,
    ).order_by(
        "display_order"
    )

    leads_qs = (
        Lead.objects
        .filter(
            organization=organization,
            pipeline=pipeline,
        )
        .select_related("stage")
    )

    if search:

        leads_qs = leads_qs.filter(
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

    filter_name = request.GET.get(
        "filter_name",
        "",
    ).strip()

    if filter_name:

        leads_qs = leads_qs.filter(
            name__icontains=filter_name
        )

    filter_phone = request.GET.get(
        "filter_phone",
        "",
    ).strip()

    if filter_phone:

        leads_qs = leads_qs.filter(
            phone__icontains=filter_phone
        )

    filter_email = request.GET.get(
        "filter_email",
        "",
    ).strip()

    if filter_email:

        leads_qs = leads_qs.filter(
            email__icontains=filter_email
        )

    filter_notes = request.GET.get(
        "filter_notes",
        "",
    ).strip()

    if filter_notes:

        leads_qs = leads_qs.filter(
            notes__icontains=filter_notes
        )

    filter_stage = request.GET.get(
        "filter_stage"
    )

    if filter_stage:

        leads_qs = leads_qs.filter(
            stage_id=filter_stage
        )

    filter_pipeline = request.GET.get(
        "filter_pipeline"
    )

    if filter_pipeline:

        selected_filter_pipeline = (
            Pipeline.objects.filter(
                organization=organization,
                id=filter_pipeline,
                is_active=True,
            ).first()
        )

        if selected_filter_pipeline:

            pipeline = (
                selected_filter_pipeline
            )

            stages = Stage.objects.filter(
                pipeline=pipeline,
                is_active=True,
            ).order_by(
                "display_order"
            )

            leads_qs = (
                Lead.objects
                .filter(
                    organization=organization,
                    pipeline=pipeline,
                )
                .select_related("stage")
            )

            if search:

                leads_qs = leads_qs.filter(
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

            if filter_name:

                leads_qs = leads_qs.filter(
                    name__icontains=filter_name
                )

            if filter_phone:

                leads_qs = leads_qs.filter(
                    phone__icontains=filter_phone
                )

            if filter_email:

                leads_qs = leads_qs.filter(
                    email__icontains=filter_email
                )

            if filter_notes:

                leads_qs = leads_qs.filter(
                    notes__icontains=filter_notes
                )

            if filter_stage:

                leads_qs = leads_qs.filter(
                    stage_id=filter_stage
                )

    # --------------------------------------------------------
    # Attribute filters
    # --------------------------------------------------------

    for key, value in request.GET.items():

        if (
            key.startswith("attr_")
            and value
        ):

            attr_key = key[
                len("attr_"):
            ]

            leads_qs = leads_qs.filter(
                **{
                    f"attributes__{attr_key}__icontains":
                    value
                }
            )

    # --------------------------------------------------------
    # Created date
    # --------------------------------------------------------

    created_after = request.GET.get(
        "filter_created_after"
    )

    if created_after:

        leads_qs = leads_qs.filter(
            created_at__date__gte=created_after
        )

    created_before = request.GET.get(
        "filter_created_before"
    )

    if created_before:

        leads_qs = leads_qs.filter(
            created_at__date__lte=created_before
        )

    # --------------------------------------------------------
    # Stage groups
    # --------------------------------------------------------

    stage_groups = []

    for i, stage in enumerate(
        stages
    ):

        stage_leads = list(
            leads_qs.filter(
                stage=stage
            )
        )

        for lead in stage_leads:

            lead.days_in_stage = (
                timezone.now()
                - lead.updated_at
            ).days

            lead.call_count = (
                lead.calls.count()
            )

            lead.next_reminder = (
                lead.reminders
                .filter(
                    status="pending"
                )
                .order_by(
                    "due_at"
                )
                .first()
            )

            lead.initials = "".join(
                [
                    p[0]
                    for p in lead.name.split()[:2]
                ]
            ).upper() or "?"

            lead_note_value = ""

            if hasattr(
                lead,
                "notes",
            ):

                lead_note_value = (
                    lead.notes or ""
                ).strip()

            latest_note = (
                LeadNote.objects
                .filter(
                    lead=lead
                )
                .order_by(
                    "-created_at"
                )
                .first()
            )

            lead.display_note = (
                latest_note
            )

            if lead_note_value:

                lead.display_note_text = (
                    lead_note_value
                )

            elif latest_note:

                lead.display_note_text = (
                    latest_note.note or ""
                )

            else:

                lead.display_note_text = ""

        theme = STAGE_THEMES[
            i % len(STAGE_THEMES)
        ]

        stage_groups.append(
            {
                "stage": stage,
                "theme": theme,
                "leads": stage_leads,
                "count": len(stage_leads),
            }
        )

    return render(
        request,
        "crm/partials/lead_table.html",
        {
            "stage_groups": stage_groups,
            "all_stages": stages,
        },
    )


# ============================================================
# PHASE 3 PART B — EDIT LEAD MODAL
# ============================================================


@crm_login_required
@require_GET
def lead_edit_modal(
    request,
    lead_id,
):

    user = request.crm_user

    organization = user.organization

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=organization,
    )

    # --------------------------------------------------------
    # PIPELINES
    #
    # Only expose pipelines the current CRM user is permitted
    # to access.
    #
    # ADMIN
    #     All active pipelines in their organization.
    #
    # AGENT
    #     Only their owned/assigned pipeline.
    # --------------------------------------------------------

    pipelines = get_user_pipelines(
        user
    )

    # --------------------------------------------------------
    # STAGES
    #
    # Stages are restricted to the lead's current pipeline.
    # --------------------------------------------------------

    stages = Stage.objects.filter(
        pipeline=lead.pipeline,
        is_active=True,
    ).order_by(
        "display_order"
    )

    # --------------------------------------------------------
    # LATEST NOTE
    # --------------------------------------------------------

    latest_note = (
        LeadNote.objects
        .filter(
            lead=lead,
        )
        .order_by(
            "-created_at",
        )
        .first()
    )

    # --------------------------------------------------------
    # LEAD NOTE TEXT
    #
    # Prefer the Lead.notes field when available.
    # Fall back to the latest LeadNote when Lead.notes is
    # empty.
    # --------------------------------------------------------

    lead_note_text = ""

    if hasattr(
        lead,
        "notes",
    ):

        lead_note_text = (
            lead.notes or ""
        ).strip()

    if (
        not lead_note_text
        and latest_note
    ):

        lead_note_text = (
            latest_note.note or ""
        )

    # --------------------------------------------------------
    # RENDER MODAL
    # --------------------------------------------------------

    return render(
        request,
        "crm/partials/lead_edit_modal.html",
        {
            "lead": lead,
            "pipelines": pipelines,
            "stages": stages,
            "latest_note": latest_note,
            "lead_note_text": lead_note_text,
        },
    )


# ============================================================
# PHASE 3 PART B — EDIT LEAD STAGE OPTIONS
# ============================================================


@crm_login_required
@require_GET
def lead_edit_stages(
    request,
):

    user = request.crm_user

    organization = user.organization

    pipeline_id = request.GET.get(
        "pipeline",
        "",
    ).strip()

    # --------------------------------------------------------
    # PIPELINE ACCESS
    #
    # ADMIN
    #     Any active pipeline in their organization.
    #
    # AGENT
    #     Only their permitted/owned pipeline.
    #
    # This intentionally reuses the existing CRM pipeline
    # access logic instead of creating a second permission
    # system.
    # --------------------------------------------------------

    allowed_pipelines = (
        get_user_pipelines(
            user
        )
        .filter(
            organization=organization,
            is_active=True,
        )
    )

    # --------------------------------------------------------
    # SELECTED PIPELINE
    #
    # The selected pipeline must be one of the pipelines
    # this CRM user is actually allowed to access.
    # --------------------------------------------------------

    pipeline = get_object_or_404(
        allowed_pipelines,
        id=pipeline_id,
    )

    # --------------------------------------------------------
    # STAGES
    #
    # IMPORTANT:
    #
    # Only active stages belonging to the selected pipeline
    # are returned.
    #
    # No stage from another pipeline can appear here.
    # --------------------------------------------------------

    stages = (
        Stage.objects
        .filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by(
            "display_order",
        )
    )

    return render(
        request,
        "crm/partials/lead_edit_stages.html",
        {
            "stages": stages,
        },
    )


@crm_login_required
@require_POST
def lead_edit_save(
    request,
    lead_id,
):

    user = request.crm_user

    organization = user.organization


    # --------------------------------------------------------
    # LOAD LEAD
    #
    # Always resolve the lead inside the current organization.
    # This preserves tenant isolation.
    # --------------------------------------------------------

    lead = get_object_or_404(
        Lead,
        id=lead_id,
        organization=organization,
    )


    # --------------------------------------------------------
    # READ FORM DATA
    # --------------------------------------------------------

    name = request.POST.get(
        "name",
        "",
    ).strip()

    email = request.POST.get(
        "email",
        "",
    ).strip()

    phone = request.POST.get(
        "phone",
        "",
    ).strip()

    pipeline_id = request.POST.get(
        "pipeline",
        "",
    ).strip()

    stage_id = request.POST.get(
        "stage",
        "",
    ).strip()

    notes = request.POST.get(
        "notes",
        "",
    ).strip()


    # --------------------------------------------------------
    # ALLOWED PIPELINES
    #
    # ADMIN
    #     Any active pipeline in their organization.
    #
    # AGENT
    #     Only their owned/assigned pipeline.
    #
    # Keep this aligned with the existing CRM permission model.
    # --------------------------------------------------------

    allowed_pipelines = get_user_pipelines(
        user
    ).filter(
        organization=organization,
        is_active=True,
    )


    # --------------------------------------------------------
    # PIPELINE
    #
    # Only allow pipelines the current CRM user is permitted
    # to access.
    # --------------------------------------------------------

    if pipeline_id:

        pipeline = get_object_or_404(
            allowed_pipelines,
            id=pipeline_id,
        )

        lead.pipeline = pipeline


    # --------------------------------------------------------
    # STAGE
    # --------------------------------------------------------

    if stage_id:

        stage = get_object_or_404(
            Stage,
            id=stage_id,
            pipeline=lead.pipeline,
            is_active=True,
        )

        lead.stage = stage


    # --------------------------------------------------------
    # BASIC FIELDS
    # --------------------------------------------------------

    lead.name = (
        name
        or lead.name
    )

    lead.email = email

    if phone:

        lead.phone = phone

    else:

        lead.phone = ""


    # --------------------------------------------------------
    # NOTES
    # --------------------------------------------------------

    if hasattr(
        lead,
        "notes",
    ):

        lead.notes = notes


    # --------------------------------------------------------
    # ATTRIBUTES
    # --------------------------------------------------------

    attributes = dict(
        lead.attributes or {}
    )

    for key, value in request.POST.items():

        if key.startswith(
            "attr_"
        ):

            attr_key = key[
                len("attr_"):
            ]

            attributes[attr_key] = value

    lead.attributes = attributes


    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    try:

        lead.full_clean()

        lead.save()


        # ----------------------------------------------------
        # MANUAL NOTE
        # ----------------------------------------------------

        latest_manual_note = (
            LeadNote.objects
            .filter(
                lead=lead,
                note_type="manual",
            )
            .order_by(
                "-created_at"
            )
            .first()
        )


        if notes:

            if latest_manual_note:

                latest_manual_note.note = (
                    notes
                )

                latest_manual_note.save(
                    update_fields=[
                        "note",
                        "updated_at",
                    ]
                )

            else:

                LeadNote.objects.create(
                    lead=lead,
                    created_by=user,
                    note=notes,
                    note_type="manual",
                )

        else:

            if latest_manual_note:

                latest_manual_note.delete()


    except DjangoValidationError as e:

        logger.exception(
            "Lead edit validation failed "
            "for lead %s",
            lead_id,
        )

        error_message = (
            e.message_dict
            if hasattr(
                e,
                "message_dict",
            )
            else e.messages
        )

        return HttpResponse(
            f"""
            <div class="text-red-600 text-sm p-4">
                Validation error: {error_message}
            </div>
            """,
            status=400,
        )


    except Exception as e:

        logger.exception(
            "Lead edit save failed "
            "for lead %s",
            lead_id,
        )

        return HttpResponse(
            f"""
            <div class="text-red-600 text-sm p-4">
                Error saving lead: {e}
            </div>
            """,
            status=400,
        )


    # --------------------------------------------------------
    # HTMX SUCCESS EVENT
    # --------------------------------------------------------

    response = HttpResponse("")

    response["HX-Trigger"] = (
        "leadUpdated"
    )

    return response


# ============================================================
# FILTER CONFIGURATION
# ============================================================


CORE_FILTER_FIELDS = [
    "Name",
    "Phone",
    "Email",
    "Notes",
    "Stage",
    "Pipeline",
]


DATE_FILTER_FIELDS = [
    "Created Date",
    "Reminder Date",
    "Stage Updated Date",
    "Pipeline Updated Date",
    "AI Qualified Date",
    "Days in stage",
]


@crm_login_required
@require_GET
def lead_filters_modal(
    request
):

    user = request.crm_user

    organization = user.organization

    pipeline_id = request.GET.get(
        "pipeline"
    )

    attribute_keys = set()

    for attrs in (
        Lead.objects
        .filter(
            organization=organization
        )
        .values_list(
            "attributes",
            flat=True,
        )
    ):

        if attrs:

            attribute_keys.update(
                attrs.keys()
            )

    return render(
        request,
        "crm/partials/lead_filters_modal.html",
        {
            "core_fields": (
                CORE_FILTER_FIELDS
            ),
            "attribute_fields": sorted(
                attribute_keys
            ),
            "date_fields": (
                DATE_FILTER_FIELDS
            ),
            "pipeline_id": pipeline_id,
        },
    )


@crm_login_required
@require_GET
def lead_filters_values(
    request
):

    fields = request.GET.getlist(
        "fields"
    )

    pipeline_id = request.GET.get(
        "pipeline"
    )

    user = request.crm_user

    organization = user.organization

    stages = (
        Stage.objects.filter(
            pipeline_id=pipeline_id,
            pipeline__organization=organization,
            is_active=True,
        )
        if pipeline_id
        else []
    )

    pipelines = Pipeline.objects.filter(
        organization=organization,
        is_active=True,
    )

    return render(
        request,
        "crm/partials/lead_filters_values.html",
        {
            "fields": fields,
            "stages": stages,
            "pipelines": pipelines,
            "pipeline_id": pipeline_id,
        },
    )
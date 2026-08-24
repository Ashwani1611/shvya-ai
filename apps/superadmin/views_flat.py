from datetime import timedelta

from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.models import OneTimeLoginToken, User
from apps.accounts.session_utils import (
    get_session_store,
    save_session_cookie,
    set_authenticated_user,
)
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import (
    APIKey,
    Organization,
    OrganizationPayment,
    OrganizationTag,
)

from .forms import (
    OrganizationCreateForm,
    OrganizationPaymentForm,
    OrganizationUpdateForm,
    OrganizationUserForm,
    OrganizationUserUpdateForm,
    PipelineCreateForm,
)

# ============================================================
# SUPER ADMIN ACCESS CONTROL
# ============================================================


def superuser_required(view_func):
    return user_passes_test(
        lambda user: user.is_authenticated and user.is_superuser,
        login_url="/superadmin/login/",
    )(view_func)


# ============================================================
# SUPER ADMIN — LOGIN
# ============================================================


def superadmin_login_view(request):
    """
    Authenticate SHVYA Superadmin users using the dedicated
    Superadmin browser session.

    This session is completely separate from:

        /admin/
        /dashboard/

    Therefore, logging a CRM user in through the one-time login
    flow will not log the Superadmin out.
    """

    # ---------------------------------------------------------
    # Load the dedicated Superadmin session
    # ---------------------------------------------------------

    superadmin_session = get_session_store(
        request,
        "superadmin",
    )

    # ---------------------------------------------------------
    # Already authenticated as Superadmin
    # ---------------------------------------------------------

    if (
        request.user.is_authenticated
        and request.user.is_superuser
    ):
        return redirect(
            "superadmin-org-list"
        )

    next_url = request.GET.get(
        "next",
        "",
    ).strip()

    # ---------------------------------------------------------
    # POST — Authenticate
    # ---------------------------------------------------------

    if request.method == "POST":

        form = AuthenticationForm(
            request,
            data=request.POST,
        )

        if form.is_valid():

            user = form.get_user()

            # -------------------------------------------------
            # Superadmin restriction
            # -------------------------------------------------

            if not user.is_superuser:

                form.add_error(
                    None,
                    "This login is restricted to Superadmin users.",
                )

            elif not user.is_active:

                form.add_error(
                    None,
                    "This Superadmin account is inactive.",
                )

            else:

                # -------------------------------------------------
                # IMPORTANT
                #
                # DO NOT use:
                #
                #
                # That uses Django's default sessionid.
                #
                # Instead authenticate this user inside the
                # dedicated Superadmin session:
                #
                #     shvya_superadmin_sessionid
                # -------------------------------------------------

                set_authenticated_user(
                    superadmin_session,
                    user,
                )

                # -------------------------------------------------
                # Determine destination
                # -------------------------------------------------

                if url_has_allowed_host_and_scheme(
                    url=next_url,
                    allowed_hosts={
                        request.get_host()
                    },
                    require_https=request.is_secure(),
                ):

                    response = redirect(
                        next_url
                    )

                else:

                    response = redirect(
                        "superadmin-org-list"
                    )

                # -------------------------------------------------
                # Save dedicated Superadmin session cookie
                # -------------------------------------------------

                save_session_cookie(
                    request,
                    response,
                    superadmin_session,
                    "superadmin",
                )

                return response

    else:

        form = AuthenticationForm(
            request,
        )

    # ---------------------------------------------------------
    # Login page
    # ---------------------------------------------------------

    return render(
        request,
        "superadmin/login.html",
        {
            "form": form,
            "next": next_url,
        },
    )

# ============================================================
# SUPER ADMIN — ORGANIZATION CONSOLE
# ============================================================


@superuser_required
def org_list_view(request):

    orgs = (
        Organization.objects
        .annotate(
            lead_count=Count(
                "leads",
                distinct=True,
            ),
            pipeline_count=Count(
                "pipelines",
                distinct=True,
            ),
        )
        .prefetch_related("tags")
    )

    search = request.GET.get(
        "search",
        "",
    ).strip()

    if search:

        orgs = orgs.filter(
            Q(name__icontains=search)
            | Q(users__email__icontains=search)
            | Q(users__phone__icontains=search)
        ).distinct()

    if request.GET.get("no_leads_created"):

        orgs = orgs.filter(
            lead_count=0,
        )

    if request.GET.get("installment_client"):

        orgs = orgs.filter(
            payment_mode=Organization.PaymentMode.PARTIAL,
        )

    plan = request.GET.get(
        "plan",
        "",
    )

    if plan:

        orgs = orgs.filter(
            package=plan,
        )

    tag_id = request.GET.get(
        "tag",
        "",
    )

    if tag_id:

        orgs = orgs.filter(
            tags__id=tag_id,
        )

    orgs = orgs.order_by(
        "-created_at",
    )

    return render(
        request,
        "superadmin/org_list.html",
        {
            "orgs": orgs,
            "all_tags": OrganizationTag.objects.all(),
            "packages": Organization.Package.choices,
            "search": search,
        },
    )


# ============================================================
# SUPER ADMIN — CREATE ORGANIZATION
# ============================================================


@superuser_required
def organization_create_view(request):

    if request.method == "GET":

        form = OrganizationCreateForm()

        return render(
            request,
            "superadmin/org_create.html",
            {
                "form": form,
            },
        )

    if request.method == "POST":

        form = OrganizationCreateForm(
            request.POST,
        )

        if form.is_valid():

            organization = form.save()

            return redirect(
                "superadmin-organization-detail",
                organization_id=organization.id,
            )

        return render(
            request,
            "superadmin/org_create.html",
            {
                "form": form,
            },
            status=400,
        )

    return redirect(
        "superadmin-org-list",
    )


# ============================================================
# SUPER ADMIN — ORGANIZATION DETAIL
# ============================================================


@superuser_required
def organization_detail_view(
    request,
    organization_id,
):

    organization = get_object_or_404(
        Organization.objects
        .select_related("assigned_poc")
        .prefetch_related(
            "tags",
            "users",
            "pipelines",
            "payments",
        ),
        pk=organization_id,
    )

    organization_users = (
        User.objects
        .filter(
            organization=organization,
        )
        .order_by(
            "-created_at",
        )
    )

    organization_form = OrganizationUpdateForm(
        instance=organization,
        organization=organization,
    )

    payment_form = OrganizationPaymentForm(
        organization=organization,
    )

    payments = organization.payments.all()

    payment_total = sum(
        payment.amount
        for payment in payments
    )

    pipeline_form = PipelineCreateForm(
        organization=organization,
    )

    return render(
        request,
        "superadmin/org_detail.html",
        {
            "organization": organization,
            "organization_form": organization_form,
            "payment_form": payment_form,
            "payments": payments,
            "payment_total": payment_total,
            "organization_users": organization_users,
            "pipeline_form": pipeline_form,
        },
    )


# ============================================================
# SUPER ADMIN — CREATE PIPELINE
# ============================================================


@superuser_required
def organization_pipeline_create_view(
    request,
    organization_id,
):
    """
    Create a new pipeline belonging to the selected
    organization.

    The organization is determined from the URL.
    """

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    # ---------------------------------------------------------
    # GET — Display Create Pipeline Form
    # ---------------------------------------------------------

    if request.method == "GET":

        form = PipelineCreateForm(
            organization=organization,
        )

        return render(
            request,
            "superadmin/pipeline_create.html",
            {
                "organization": organization,
                "form": form,
            },
        )

    # ---------------------------------------------------------
    # POST — Create Pipeline
    # ---------------------------------------------------------

    if request.method == "POST":

        form = PipelineCreateForm(
            request.POST,
            organization=organization,
        )

        if form.is_valid():

            form.save()

            return redirect(
                "superadmin-organization-detail",
                organization_id=organization.id,
            )

        return render(
            request,
            "superadmin/pipeline_create.html",
            {
                "organization": organization,
                "form": form,
            },
            status=400,
        )

    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


# ============================================================
# SUPER ADMIN — EDIT PIPELINE
# ============================================================


@superuser_required
def organization_pipeline_update_view(
    request,
    organization_id,
    pipeline_id,
):
    """
    Edit an existing pipeline belonging to the selected
    organization.

    The pipeline must belong to the organization from the URL.
    This prevents cross-organization editing.
    """

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    pipeline = get_object_or_404(
        Pipeline,
        pk=pipeline_id,
        organization=organization,
    )

    # ---------------------------------------------------------
    # GET — Display Edit Pipeline Form
    # ---------------------------------------------------------

    if request.method == "GET":

        form = PipelineCreateForm(
            instance=pipeline,
            organization=organization,
        )

        return render(
            request,
            "superadmin/pipeline_edit.html",
            {
                "organization": organization,
                "pipeline": pipeline,
                "form": form,
            },
        )

    # ---------------------------------------------------------
    # POST — Update Pipeline
    # ---------------------------------------------------------

    if request.method == "POST":

        form = PipelineCreateForm(
            request.POST,
            instance=pipeline,
            organization=organization,
        )

        if form.is_valid():

            form.save()

            return redirect(
                "superadmin-organization-detail",
                organization_id=organization.id,
            )

        return render(
            request,
            "superadmin/pipeline_edit.html",
            {
                "organization": organization,
                "pipeline": pipeline,
                "form": form,
            },
            status=400,
        )

    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


# ============================================================
# SUPER ADMIN — DELETE PIPELINE
# ============================================================


@superuser_required
def organization_pipeline_delete_view(
    request,
    organization_id,
    pipeline_id,
):
    """
    Delete an existing pipeline belonging to the selected
    organization.

    The pipeline must belong to the organization from the URL.

    Only POST requests are allowed.
    """

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    pipeline = get_object_or_404(
        Pipeline,
        pk=pipeline_id,
        organization=organization,
    )

    # ---------------------------------------------------------
    # Only POST is allowed
    # ---------------------------------------------------------

    if request.method != "POST":

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    # ---------------------------------------------------------
    # Delete Pipeline
    # ---------------------------------------------------------

    pipeline.delete()

    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


# ============================================================
# SUPER ADMIN — UPDATE ORGANIZATION
# ============================================================


@superuser_required
def organization_update_view(
    request,
    organization_id,
):

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    if request.method == "GET":

        form = OrganizationUpdateForm(
            instance=organization,
            organization=organization,
        )

        return render(
            request,
            "superadmin/org_edit.html",
            {
                "organization": organization,
                "form": form,
            },
        )

    if request.method == "POST":

        form = OrganizationUpdateForm(
            request.POST,
            instance=organization,
            organization=organization,
        )

        if form.is_valid():

            form.save()

            return redirect(
                "superadmin-organization-detail",
                organization_id=organization.id,
            )

        return render(
            request,
            "superadmin/org_edit.html",
            {
                "organization": organization,
                "form": form,
            },
            status=400,
        )

    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


# ============================================================
# SUPER ADMIN — CREATE ORGANIZATION USER
# ============================================================


@superuser_required
def organization_user_create_view(
    request,
    organization_id,
):
    """
    Create a new user inside the selected organization.

    The organization is attached to the User instance BEFORE
    form validation because the User model requires every
    non-superadmin user to belong to an organization.

    Superadmin users cannot be created through this endpoint.
    """

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    # ---------------------------------------------------------
    # Only POST is allowed
    # ---------------------------------------------------------

    if request.method != "POST":
        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    # ---------------------------------------------------------
    # Bind submitted form data
    # ---------------------------------------------------------

    form = OrganizationUserForm(
        request.POST,
    )

    # ---------------------------------------------------------
    # IMPORTANT
    #
    # The organization MUST be attached BEFORE validation.
    #
    # User.clean() requires organization for non-superadmin
    # users. Passing organization only to form.save() is too late.
    # ---------------------------------------------------------

    form.instance.organization = organization

    # ---------------------------------------------------------
    # Validate and create user
    # ---------------------------------------------------------

    if form.is_valid():

        user = form.save(
            organization=organization,
        )

        user.refresh_from_db()

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    # ---------------------------------------------------------
    # Validation failed
    #
    # Render the organization detail page again and show the
    # validation errors in the Create User form.
    # ---------------------------------------------------------

    organization_users = (
        User.objects
        .filter(
            organization=organization,
        )
        .order_by(
            "-created_at",
        )
    )

    organization_form = OrganizationUpdateForm(
        instance=organization,
        organization=organization,
    )

    payment_form = OrganizationPaymentForm(
        organization=organization,
    )

    payments = organization.payments.all()

    payment_total = sum(
        payment.amount
        for payment in payments
    )

    pipeline_form = PipelineCreateForm(
        organization=organization,
    )

    return render(
        request,
        "superadmin/org_detail.html",
        {
            "organization": organization,
            "organization_form": organization_form,
            "payment_form": payment_form,
            "payment_total": payment_total,
            "organization_user_form": form,
            "organization_users": organization_users,
            "pipeline_form": pipeline_form,
        },
        status=400,
    )


# ============================================================
# SUPER ADMIN — EDIT ORGANIZATION USER
# ============================================================


@superuser_required
def organization_user_update_view(
    request,
    organization_id,
    user_id,
):

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    user = get_object_or_404(
        User,
        pk=user_id,
        organization=organization,
    )

    if request.method == "POST":

        form = OrganizationUserUpdateForm(
            request.POST,
            instance=user,
            organization=organization,
        )

        if form.is_valid():

            form.save()

            return redirect(
                "superadmin-organization-detail",
                organization_id=organization.id,
            )

    else:

        form = OrganizationUserUpdateForm(
            instance=user,
            organization=organization,
        )

    return render(
        request,
        "superadmin/organization_user_edit.html",
        {
            "organization": organization,
            "user": user,
            "form": form,
        },
    )


# ============================================================
# SUPER ADMIN — ENABLE / DISABLE ORGANIZATION USER
# ============================================================


@superuser_required
def organization_user_toggle_active_view(
    request,
    organization_id,
    user_id,
):

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    user = get_object_or_404(
        User,
        pk=user_id,
        organization=organization,
    )

    if request.method != "POST":

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    is_active = request.POST.get(
        "is_active",
    )

    if is_active == "1":

        user.is_active = True

    elif is_active == "0":

        user.is_active = False

    else:

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    user.save(
        update_fields=["is_active"],
    )

    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


# ============================================================
# SUPER ADMIN — RESET ORGANIZATION USER PASSWORD
# ============================================================


@superuser_required
def organization_user_reset_password_view(
    request,
    organization_id,
):
    """
    Reset the password of an existing organization user.

    Security:
        - Superadmin access is required.
        - The organization is determined from the URL.
        - The selected user must belong to that organization.
        - Superadmin users cannot be selected.
        - Only POST requests change a password.
    """

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    organization_users = (
        User.objects
        .filter(
            organization=organization,
            is_superuser=False,
        )
        .order_by(
            "email",
        )
    )

    selected_user = None
    reset_password_form = None

    if request.method == "POST":

        user_id = request.POST.get(
            "user_id",
            "",
        ).strip()

        if user_id:

            selected_user = get_object_or_404(
                User,
                pk=user_id,
                organization=organization,
                is_superuser=False,
            )

            reset_password_form = SetPasswordForm(
                selected_user,
                request.POST,
            )

            if reset_password_form.is_valid():

                reset_password_form.save()

                return redirect(
                    "superadmin-organization-detail",
                    organization_id=organization.id,
                )

    else:

        reset_password_form = None

    organization_form = OrganizationUpdateForm(
        instance=organization,
        organization=organization,
    )

    payment_form = OrganizationPaymentForm(
        organization=organization,
    )

    payments = organization.payments.all()

    payment_total = sum(
        payment.amount
        for payment in payments
    )

    pipeline_form = PipelineCreateForm(
        organization=organization,
    )

    return render(
        request,
        "superadmin/org_detail.html",
        {
            "organization": organization,
            "organization_form": organization_form,
            "payment_form": payment_form,
            "payments": payments,
            "payment_total": payment_total,
            "organization_users": organization_users,
            "pipeline_form": pipeline_form,
            "reset_password_users": organization_users,
            "reset_password_form": reset_password_form,
            "reset_password_selected_user": selected_user,
        },
        status=400 if request.method == "POST" else 200,
    )


# ============================================================
# SUPER ADMIN — GENERATE LOGIN LINK
# ============================================================


@superuser_required
def organization_generate_login_link_view(
    request,
    organization_id,
):
    """
    Generate a secure one-time login link for the owner
    of the organization's active "Leads" pipeline.

    Flow:

        Organization
            ↓
        Active "Leads" pipeline
            ↓
        Pipeline owner
            ↓
        Active organization user
            ↓
        Secure random token
            ↓
        5-minute expiry
            ↓
        /one-time-login?token=...

    Important:

        PipelinePermission is NOT used for login-link generation.

        The owner of the "Leads" pipeline is the user who
        receives the generated login link.

    Rules:

        - Only SHVYA superadmin can generate the link.
        - Request must be POST.
        - Pipeline name must be exactly "Leads".
        - Pipeline must belong to the selected organization.
        - Pipeline must be active.
        - Pipeline must have an owner.
        - Owner must belong to the same organization.
        - Owner must be active.
        - Owner cannot be a superadmin.
    """

    # ---------------------------------------------------------
    # Only POST is allowed
    # ---------------------------------------------------------

    if request.method != "POST":

        return JsonResponse(
            {
                "success": False,
                "error": "POST request required.",
            },
            status=405,
        )

    # ---------------------------------------------------------
    # Find organization
    # ---------------------------------------------------------

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    # ---------------------------------------------------------
    # Find the active "Leads" pipeline
    # ---------------------------------------------------------

    pipeline = (
        Pipeline.objects
        .select_related(
            "owner",
            "organization",
        )
        .filter(
            organization=organization,
            name="Leads",
            is_active=True,
        )
        .first()
    )

    if pipeline is None:

        return JsonResponse(
            {
                "success": False,
                "error": (
                    'No active "Leads" pipeline exists '
                    "for this organization."
                ),
            },
            status=404,
        )

    # ---------------------------------------------------------
    # Pipeline must have an owner
    # ---------------------------------------------------------

    if pipeline.owner is None:

        return JsonResponse(
            {
                "success": False,
                "error": (
                    'The active "Leads" pipeline does not '
                    "have an owner assigned."
                ),
            },
            status=400,
        )

    # ---------------------------------------------------------
    # Get pipeline owner
    # ---------------------------------------------------------

    user = pipeline.owner

    # ---------------------------------------------------------
    # Security — owner must belong to the same organization
    # ---------------------------------------------------------

    if user.organization_id != organization.id:

        return JsonResponse(
            {
                "success": False,
                "error": (
                    'The owner of the "Leads" pipeline does '
                    "not belong to this organization."
                ),
            },
            status=400,
        )

    # ---------------------------------------------------------
    # Owner cannot be a superadmin
    # ---------------------------------------------------------

    if user.is_superuser:

        return JsonResponse(
            {
                "success": False,
                "error": (
                    'The owner of the "Leads" pipeline '
                    "cannot be a superadmin."
                ),
            },
            status=400,
        )

    # ---------------------------------------------------------
    # Owner must be active
    # ---------------------------------------------------------

    if not user.is_active:

        return JsonResponse(
            {
                "success": False,
                "error": (
                    'The owner of the "Leads" pipeline '
                    "is currently inactive."
                ),
            },
            status=400,
        )

    # ---------------------------------------------------------
    # Expire previous unused login tokens for this user
    #
    # This prevents multiple active login links from remaining
    # valid simultaneously.
    # ---------------------------------------------------------

    now = timezone.now()

    OneTimeLoginToken.objects.filter(
        user=user,
        organization=organization,
        used_at__isnull=True,
        expires_at__gt=now,
    ).update(
        expires_at=now,
    )

    # ---------------------------------------------------------
    # Generate new secure token
    #
    # Token is valid for 5 minutes.
    # The model stores only the SHA-256 hash.
    # ---------------------------------------------------------

    expires_at = now + timedelta(
        minutes=5,
    )

    token_record, raw_token = OneTimeLoginToken.create_token(
        user=user,
        organization=organization,
        expires_at=expires_at,
    )

    # ---------------------------------------------------------
    # Build one-time login URL
    # ---------------------------------------------------------

    login_path = reverse(
        "one-time-login",
    )

    login_url = request.build_absolute_uri(
        f"{login_path}?token={raw_token}"
    )

    # ---------------------------------------------------------
    # Return response for Generate Login Link modal
    # ---------------------------------------------------------

    return JsonResponse(
        {
            "success": True,
            "message": "Login link generated successfully.",
            "login_url": login_url,
            "expires_at": expires_at.isoformat(),
            "expires_in_seconds": 300,
            "organization_id": str(
                organization.id,
            ),
            "organization_name": organization.name,
            "pipeline": pipeline.name,
            "user_id": str(
                user.id,
            ),
            "user_name": user.name,
            "user_email": user.email,
        }
    )


# ============================================================
# SUPER ADMIN — ADD PAYMENT
# ============================================================


@superuser_required
def organization_payment_create_view(
    request,
    organization_id,
):

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    if request.method != "POST":

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    form = OrganizationPaymentForm(
        request.POST,
        organization=organization,
    )

    if form.is_valid():

        form.save()

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    organization_users = (
        User.objects
        .filter(
            organization=organization,
        )
        .order_by(
            "-created_at",
        )
    )

    organization_form = OrganizationUpdateForm(
        instance=organization,
        organization=organization,
    )

    payments = organization.payments.all()

    payment_total = sum(
        payment.amount
        for payment in payments
    )

    pipeline_form = PipelineCreateForm(
        organization=organization,
    )

    return render(
        request,
        "superadmin/org_detail.html",
        {
            "organization": organization,
            "organization_form": organization_form,
            "payment_form": form,
            "payments": payments,
            "payment_total": payment_total,
            "organization_users": organization_users,
            "pipeline_form": pipeline_form,
        },
        status=400,
    )


# ============================================================
# SUPER ADMIN — EDIT PAYMENT
# ============================================================


@superuser_required
def organization_payment_update_view(
    request,
    organization_id,
    payment_id,
):

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    payment = get_object_or_404(
        OrganizationPayment,
        pk=payment_id,
        organization=organization,
    )

    if request.method != "POST":

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    form = OrganizationPaymentForm(
        request.POST,
        instance=payment,
        organization=organization,
    )

    if form.is_valid():

        form.save()

        return redirect(
            "superadmin-organization-detail",
            organization_id=organization.id,
        )

    organization_users = (
        User.objects
        .filter(
            organization=organization,
        )
        .order_by(
            "-created_at",
        )
    )

    organization_form = OrganizationUpdateForm(
        instance=organization,
        organization=organization,
    )

    payments = organization.payments.all()

    payment_total = sum(
        item.amount
        for item in payments
    )

    pipeline_form = PipelineCreateForm(
        organization=organization,
    )

    return render(
        request,
        "superadmin/org_detail.html",
        {
            "organization": organization,
            "organization_form": organization_form,
            "payment_form": form,
            "payments": payments,
            "payment_total": payment_total,
            "edit_payment": payment,
            "organization_users": organization_users,
            "pipeline_form": pipeline_form,
        },
        status=400,
    )


# ============================================================
# SUPER ADMIN — DELETE PAYMENT
# ============================================================


@superuser_required
def organization_payment_delete_view(
    request,
    organization_id,
    payment_id,
):

    organization = get_object_or_404(
        Organization,
        pk=organization_id,
    )

    payment = get_object_or_404(
        OrganizationPayment,
        pk=payment_id,
        organization=organization,
    )

    if request.method == "POST":

        payment.delete()

    return redirect(
        "superadmin-organization-detail",
        organization_id=organization.id,
    )


# ============================================================
# SHVYA ADMIN — GLOBAL SEARCH
# ============================================================


@superuser_required
def admin_global_search(request):
    """
    Global search endpoint for the SHVYA Superadmin dashboard.

    Searches across:

        Organizations
        Users
        Leads
        Pipelines
        API Keys
    """

    query = request.GET.get(
        "q",
        "",
    ).strip()

    if not query:

        return JsonResponse(
            {
                "query": "",
                "results": [],
            }
        )

    results = []

    # =========================================================
    # ORGANIZATIONS
    # =========================================================

    organizations = (
        Organization.objects
        .filter(
            Q(name__icontains=query)
        )
        .order_by("name")[:5]
    )

    for organization in organizations:

        results.append(
            {
                "type": "organization",
                "label": "Organization",
                "name": organization.name,
                "url": reverse(
                    "admin:organizations_organization_change",
                    args=[organization.pk],
                ),
            }
        )

    # =========================================================
    # USERS
    # =========================================================

    users = (
        User.objects
        .filter(
            Q(name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
        )
        .order_by("email")[:5]
    )

    for user in users:

        display_name = (
            user.name.strip()
            if user.name
            else user.email
        )

        results.append(
            {
                "type": "user",
                "label": "User",
                "name": display_name,
                "url": reverse(
                    "admin:accounts_user_change",
                    args=[user.pk],
                ),
            }
        )

    # =========================================================
    # LEADS
    # =========================================================

    leads = (
        Lead.objects
        .filter(
            Q(name__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
        )
        .order_by("name")[:5]
    )

    for lead in leads:

        results.append(
            {
                "type": "lead",
                "label": "Lead",
                "name": lead.name,
                "url": reverse(
                    "admin:crm_lead_change",
                    args=[lead.pk],
                ),
            }
        )

    # =========================================================
    # PIPELINES
    # =========================================================

    pipelines = (
        Pipeline.objects
        .filter(
            Q(name__icontains=query)
        )
        .order_by("name")[:5]
    )

    for pipeline in pipelines:

        results.append(
            {
                "type": "pipeline",
                "label": "Pipeline",
                "name": pipeline.name,
                "url": reverse(
                    "admin:crm_pipeline_change",
                    args=[pipeline.pk],
                ),
            }
        )

    # =========================================================
    # API KEYS
    # =========================================================

    api_keys = (
        APIKey.objects
        .filter(
            Q(name__icontains=query)
            | Q(key_prefix__icontains=query)
        )
        .select_related("organization")
        .order_by("name")[:5]
    )

    for api_key in api_keys:

        results.append(
            {
                "type": "api_key",
                "label": "API Key",
                "name": api_key.name,
                "organization": api_key.organization.name,
                "url": reverse(
                    "admin:organizations_apikey_change",
                    args=[api_key.pk],
                ),
            }
        )

    return JsonResponse(
        {
            "query": query,
            "results": results[:20],
        }
    )

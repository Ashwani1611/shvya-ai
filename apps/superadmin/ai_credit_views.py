from __future__ import annotations

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.ai_engagement.models import (
    AICreditTransaction,
    AICreditWallet,
)
from apps.ai_engagement.services.credits import (
    AICreditError,
    AICreditService,
)
from apps.organizations.models import Organization

from .views_flat import superuser_required


@superuser_required
@require_http_methods(["GET"])
def ai_credit_overview_view(request):
    """Show one manually-funded AI wallet for every organization."""

    search = (request.GET.get("search") or "").strip()

    organizations = Organization.objects.all()
    if search:
        organizations = organizations.filter(
            Q(name__icontains=search)
            | Q(users__email__icontains=search)
        ).distinct()

    organization_ids = list(organizations.values_list("id", flat=True))
    existing_ids = set(
        AICreditWallet.objects.filter(
            organization_id__in=organization_ids,
        ).values_list("organization_id", flat=True)
    )
    missing = [
        AICreditWallet(organization_id=organization_id, balance=0)
        for organization_id in organization_ids
        if organization_id not in existing_ids
    ]
    if missing:
        # Creating an empty wallet is not a credit allocation. New and existing
        # organizations still remain at zero until Superadmin manually funds it.
        AICreditWallet.objects.bulk_create(missing, ignore_conflicts=True)

    organizations = (
        organizations
        .select_related("ai_credit_wallet")
        .order_by("-created_at")
    )

    return render(
        request,
        "superadmin/ai_credit_overview.html",
        {
            "organizations": organizations,
            "search": search,
        },
    )


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_ai_credit_view(request, organization_id):
    """Manually manage one organization's AI wallet and audit ledger."""

    organization = get_object_or_404(Organization, pk=organization_id)
    wallet = AICreditService.ensure_wallet(organization)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        try:
            if action == "add":
                amount = int(request.POST.get("amount") or 0)
                reason = request.POST.get("reason") or ""
                AICreditService.add_manual_credits(
                    organization=organization,
                    amount=amount,
                    reason=reason,
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Added {amount:,} AI credits to {organization.name}.",
                )

            elif action == "deduct":
                amount = int(request.POST.get("amount") or 0)
                reason = request.POST.get("reason") or ""
                AICreditService.deduct_manual_credits(
                    organization=organization,
                    amount=amount,
                    reason=reason,
                    actor=request.user,
                )
                messages.success(
                    request,
                    f"Deducted {amount:,} AI credits from {organization.name}.",
                )

            elif action == "block":
                blocked = (request.POST.get("blocked") or "") == "1"
                AICreditService.set_blocked(
                    organization=organization,
                    blocked=blocked,
                )
                messages.success(
                    request,
                    (
                        "AI usage blocked for this organization."
                        if blocked
                        else "AI usage unblocked for this organization."
                    ),
                )

            elif action == "threshold":
                threshold = int(request.POST.get("threshold") or 0)
                AICreditService.set_low_credit_threshold(
                    organization=organization,
                    threshold=threshold,
                )
                messages.success(
                    request,
                    f"Low-credit threshold updated to {threshold:,} credits.",
                )

            else:
                raise AICreditError("Unknown AI credit action.")

        except (TypeError, ValueError):
            messages.error(request, "Enter a valid whole-number AI credit amount.")
        except AICreditError as exc:
            messages.error(request, str(exc))

        return redirect(
            "superadmin-organization-ai-credits",
            organization_id=organization.id,
        )

    wallet.refresh_from_db()
    now = timezone.now()
    usage = wallet.transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
    )
    today_total = usage.filter(
        created_at__date=timezone.localdate(),
    ).aggregate(total=Sum("amount"))["total"] or 0
    month_total = usage.filter(
        created_at__year=now.year,
        created_at__month=now.month,
    ).aggregate(total=Sum("amount"))["total"] or 0

    transactions = wallet.transactions.all()[:100]
    active_reservations = wallet.reservations.filter(
        status="active",
    ).order_by("-created_at")[:25]

    return render(
        request,
        "superadmin/ai_credit_detail.html",
        {
            "organization": organization,
            "wallet": wallet,
            "transactions": transactions,
            "active_reservations": active_reservations,
            "used_today": max(-int(today_total), 0),
            "used_this_month": max(-int(month_total), 0),
        },
    )

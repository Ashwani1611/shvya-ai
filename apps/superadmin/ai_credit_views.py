from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
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


AI_FEATURE_LABELS = {
    "engagement": "AI Engagement",
    "qualification": "Qualification",
    "internal_summary": "Conversation Summary",
    "knowledge_embedding": "Knowledge Embedding",
    "knowledge_retrieval": "Knowledge Retrieval",
    "playground": "AI Playground",
    "playground_retrieval": "Playground Retrieval",
    "embedding": "Knowledge Embedding",
    "other": "Other AI Usage",
}

USAGE_RANGE_PRESETS = {"today", "7d", "30d", "month", "all", "custom"}


def _feature_label(feature: str) -> str:
    normalized = (feature or "other").strip().lower()
    return AI_FEATURE_LABELS.get(
        normalized,
        normalized.replace("_", " ").strip().title() or "Other AI Usage",
    )


def _resolve_usage_date_filter(request):
    """Resolve quick presets or an explicit inclusive date range."""

    today = timezone.localdate()
    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    preset = (request.GET.get("range") or "30d").strip().lower()
    if preset not in USAGE_RANGE_PRESETS:
        preset = "30d"

    start_date = parse_date(raw_from) if raw_from else None
    end_date = parse_date(raw_to) if raw_to else None

    # Explicit dates always take precedence over a quick preset.
    if raw_from or raw_to:
        preset = "custom"
    elif preset == "today":
        start_date = today
        end_date = today
    elif preset == "7d":
        start_date = today - timedelta(days=6)
        end_date = today
    elif preset == "30d":
        start_date = today - timedelta(days=29)
        end_date = today
    elif preset == "month":
        start_date = today.replace(day=1)
        end_date = today
    elif preset == "all":
        start_date = None
        end_date = None

    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date

    if start_date and end_date:
        if start_date == end_date:
            label = start_date.strftime("%d %b %Y")
        else:
            label = (
                f"{start_date.strftime('%d %b %Y')} – "
                f"{end_date.strftime('%d %b %Y')}"
            )
    elif start_date:
        label = f"From {start_date.strftime('%d %b %Y')}"
    elif end_date:
        label = f"Through {end_date.strftime('%d %b %Y')}"
    else:
        label = "All time"

    return {
        "preset": preset,
        "start_date": start_date,
        "end_date": end_date,
        "start_value": start_date.isoformat() if start_date else "",
        "end_value": end_date.isoformat() if end_date else "",
        "label": label,
    }


def _filter_transactions_for_dates(queryset, date_filter):
    start_date = date_filter["start_date"]
    end_date = date_filter["end_date"]

    if start_date:
        queryset = queryset.filter(created_at__date__gte=start_date)
    if end_date:
        queryset = queryset.filter(created_at__date__lte=end_date)
    return queryset


def _build_usage_report(transactions):
    """Build signed credit totals and action-level AI usage for one date range."""

    credits_added = transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.MANUAL_CREDIT,
    ).aggregate(total=Sum("amount"))["total"] or 0
    manual_debits = transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.MANUAL_DEBIT,
    ).aggregate(total=Sum("amount"))["total"] or 0
    ai_usage_total = transactions.filter(
        transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
    ).aggregate(total=Sum("amount"))["total"] or 0
    net_change = transactions.aggregate(total=Sum("amount"))["total"] or 0

    usage_rows = []
    grouped_usage = (
        transactions.filter(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
        )
        .values("feature")
        .annotate(total=Sum("amount"))
        .order_by("feature")
    )
    for row in grouped_usage:
        amount = int(row["total"] or 0)
        if amount == 0:
            continue
        usage_rows.append(
            {
                "feature": row["feature"] or "other",
                "label": _feature_label(row["feature"]),
                "amount": amount,
                "signed_display": f"{amount:+,}",
            }
        )

    # Highest-consuming AI action first, regardless of the underlying feature key.
    usage_rows.sort(key=lambda row: abs(row["amount"]), reverse=True)

    credits_added = int(credits_added)
    manual_debits = int(manual_debits)
    ai_usage_total = int(ai_usage_total)
    net_change = int(net_change)

    return {
        "credits_added": max(credits_added, 0),
        "credits_added_display": f"{max(credits_added, 0):,}",
        "manual_deducted": max(-manual_debits, 0),
        "manual_deducted_display": f"{max(-manual_debits, 0):,}",
        "ai_used": max(-ai_usage_total, 0),
        "ai_used_display": f"{max(-ai_usage_total, 0):,}",
        "net_change": net_change,
        "net_change_display": f"{net_change:+,}",
        "usage_rows": usage_rows,
    }


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

    date_filter = _resolve_usage_date_filter(request)
    filtered_transactions = _filter_transactions_for_dates(
        wallet.transactions.all(),
        date_filter,
    )
    usage_report = _build_usage_report(filtered_transactions)

    transactions = filtered_transactions[:100]
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
            "date_filter": date_filter,
            "usage_report": usage_report,
        },
    )

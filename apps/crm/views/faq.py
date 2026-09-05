from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render

from apps.ai_engagement.services.faq import (
    FAQService,
    FAQServiceError,
)
from apps.crm.authentication import crm_login_required


@crm_login_required
def faq_view(request):
    """
    CRM dashboard page for organization-level FAQ management.

    CRM authentication is resolved through the dedicated CRM
    session. FAQ ownership is always derived from request.crm_user.
    """

    user = request.crm_user
    organization = user.organization

    service = FAQService()

    if request.method == "POST":

        action = request.POST.get(
            "action",
            "",
        )

        # =========================================================
        # CREATE FAQ
        # =========================================================

        if action == "create":

            question = request.POST.get(
                "question",
                "",
            ).strip()

            answer = request.POST.get(
                "answer",
                "",
            ).strip()

            if not question:
                messages.error(
                    request,
                    "FAQ question cannot be empty.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            if not answer:
                messages.error(
                    request,
                    "FAQ answer cannot be empty.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            try:
                service.create(
                    organization=organization,
                    question=question,
                    answer=answer,
                    is_active=True,
                )

            except FAQServiceError as exc:
                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            messages.success(
                request,
                "FAQ added successfully.",
            )

            return redirect(
                "crm-knowledge-base-faq",
            )

        # =========================================================
        # UPDATE FAQ
        # =========================================================

        if action == "update":

            faq_id = request.POST.get(
                "faq_id",
                "",
            )

            question = request.POST.get(
                "question",
                "",
            ).strip()

            answer = request.POST.get(
                "answer",
                "",
            ).strip()

            if not faq_id:
                messages.error(
                    request,
                    "FAQ ID is required.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            if not question:
                messages.error(
                    request,
                    "FAQ question cannot be empty.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            if not answer:
                messages.error(
                    request,
                    "FAQ answer cannot be empty.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            try:
                service.update(
                    organization=organization,
                    faq_id=faq_id,
                    data={
                        "question": question,
                        "answer": answer,
                    },
                )

            except FAQServiceError as exc:
                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            messages.success(
                request,
                "FAQ updated successfully.",
            )

            return redirect(
                "crm-knowledge-base-faq",
            )

        # =========================================================
        # ACTIVATE / DEACTIVATE FAQ
        # =========================================================

        if action == "toggle_status":

            faq_id = request.POST.get(
                "faq_id",
                "",
            )

            status_value = request.POST.get(
                "is_active",
                "",
            )

            is_active = status_value == "true"

            if not faq_id:
                messages.error(
                    request,
                    "FAQ ID is required.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            try:
                service.update(
                    organization=organization,
                    faq_id=faq_id,
                    data={
                        "is_active": is_active,
                    },
                )

            except FAQServiceError as exc:
                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            if is_active:
                messages.success(
                    request,
                    "FAQ activated.",
                )
            else:
                messages.success(
                    request,
                    "FAQ archived.",
                )

            return redirect(
                "crm-knowledge-base-faq",
            )

        # =========================================================
        # DELETE FAQ
        # =========================================================

        if action == "delete":

            faq_id = request.POST.get(
                "faq_id",
                "",
            )

            if not faq_id:
                messages.error(
                    request,
                    "FAQ ID is required.",
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            try:
                service.delete(
                    organization=organization,
                    faq_id=faq_id,
                )

            except FAQServiceError as exc:
                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-faq",
                )

            messages.success(
                request,
                "FAQ deleted successfully.",
            )

            return redirect(
                "crm-knowledge-base-faq",
            )

        messages.error(
            request,
            "Unknown FAQ action.",
        )

        return redirect(
            "crm-knowledge-base-faq",
        )

    # =============================================================
    # GET
    # =============================================================

    show_archived = (
        request.GET.get(
            "show_archived",
            "true",
        ).lower()
        == "true"
    )

    try:
        faqs = service.list(
            organization=organization,
            active_only=not show_archived,
        )

    except FAQServiceError as exc:
        messages.error(
            request,
            str(exc),
        )

        faqs = []

    return render(
        request,
        "crm/knowledge_base/faq.html",
        {
            "faqs": faqs,
            "show_archived": show_archived,
            "organization": organization,
        },
    )
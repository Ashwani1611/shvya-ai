from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import render

from apps.core.booking import BAC_ORGANIZATION_ID
from apps.core.models import MarketingBookingRequest
from apps.superadmin.views_flat import superuser_required


@superuser_required
def bac_list(request):
    bookings = MarketingBookingRequest.objects.filter(
        lead__organization_id=BAC_ORGANIZATION_ID
    ).select_related("lead", "lead__stage")
    query = request.GET.get("q", "").strip()[:200]
    if query:
        bookings = bookings.filter(Q(name__icontains=query) | Q(email__icontains=query)
                                   | Q(phone__icontains=query) | Q(company__icontains=query))
    return render(request, "superadmin/bac.html", {
        "page_obj": Paginator(bookings, 25).get_page(request.GET.get("page")),
        "query": query, "bac_organization_id": BAC_ORGANIZATION_ID,
    })

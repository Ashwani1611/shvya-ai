from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.organizations.models import Organization
from .views_flat import superuser_required


@superuser_required
@require_GET
def organization_lookup(request):
    query = request.GET.get("q", "").strip()[:100]
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = Organization.objects.filter(
        Q(name__icontains=query) | Q(users__email__icontains=query) | Q(users__phone__icontains=query)
    ).distinct().order_by("name").values("id", "name", "is_active")[:10]
    response = JsonResponse({"results": [{
        "name": row["name"], "active": row["is_active"],
        "url": reverse("superadmin-organization-detail", args=[row["id"]]),
    } for row in rows]})
    response["Cache-Control"] = "private, no-store"
    return response

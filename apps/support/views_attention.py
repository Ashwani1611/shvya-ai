"""Read-only dashboard notification boundary; reuse support access and privacy."""
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .access import portal_required
from .attention import attention_count
from .views import private


@never_cache
@portal_required()
@require_GET
def attention_status(request):
    return private(JsonResponse({"count": attention_count(request.user)}))

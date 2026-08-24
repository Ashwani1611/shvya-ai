#!/bin/bash
set -e
GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[DONE]${NC} $1"; }
info() { echo -e "${BLUE}[....]${NC} $1"; }

# 1. Fix core/exceptions.py
info "Fixing core/exceptions.py..."
cat > core/exceptions.py << 'PYEOF'
import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)

def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            "error": True,
            "status_code": response.status_code,
            "detail": response.data,
        }
    else:
        logger.exception("Unhandled exception", exc_info=exc)
        response = Response(
            {"error": True, "status_code": 500, "detail": "Internal server error."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return response
PYEOF
log "core/exceptions.py"

# 2. Wire api/v1/router.py
info "Wiring api/v1/router.py..."
cat > api/v1/router.py << 'PYEOF'
from django.urls import path, include

urlpatterns = [
    path("crm/",        include("apps.crm.urls.api_v1")),
    path("triggers/",   include("apps.triggers.urls.api_v1")),
    path("telephony/",  include("apps.telephony.urls.api_v1")),
    path("knowledge/",  include("apps.knowledge.urls.api_v1")),
]
PYEOF
log "api/v1/router.py"

# 3. Add missing tasks.py files
info "Adding missing tasks.py files..."
for app in accounts analytics channels integrations organizations superadmin teams; do
    if [ ! -f apps/$app/tasks.py ]; then
        cat > apps/$app/tasks.py << PYEOF
from config.celery import app

# Add Celery tasks for $app here
PYEOF
        log "apps/$app/tasks.py"
    fi
done

# 4. Create views/ packages for flat-views apps (keep views.py, add package alongside)
info "Creating views/ packages..."
for app in accounts analytics calls channels copilot followups integrations organizations superadmin teams; do
    if [ ! -d apps/$app/views ]; then
        mkdir -p apps/$app/views
        # __init__.py re-exports everything from flat views.py for backward compat
        cat > apps/$app/views/__init__.py << PYEOF
# Re-exports from legacy views.py — migrate views here incrementally
from apps.$app.views_flat import *  # noqa
PYEOF
        # rename flat views.py to views_flat.py so the package doesn't conflict
        mv apps/$app/views.py apps/$app/views_flat.py
        log "apps/$app/views/ (views.py → views_flat.py)"
    fi
done

# 5. Create urls/ packages for apps missing them
info "Creating urls/ packages..."
for app in analytics calls copilot followups integrations organizations teams; do
    if [ ! -d apps/$app/urls ]; then
        mkdir -p apps/$app/urls
        touch apps/$app/urls/__init__.py
        cat > apps/$app/urls/api_v1.py << PYEOF
from django.urls import path

urlpatterns = [
    # Add $app API routes here
]
PYEOF
        log "apps/$app/urls/api_v1.py"
    fi
done

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Architecture fix complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Next: python manage.py check"

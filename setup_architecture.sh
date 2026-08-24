#!/bin/bash
set -e
GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[CREATE]${NC} $1"; }
info() { echo -e "${BLUE}[INFO]${NC}   $1"; }
warn() { echo -e "${YELLOW}[SKIP]${NC}   $1 (already exists)"; }
mk() { if [ ! -f "$1" ]; then mkdir -p "$(dirname "$1")"; touch "$1"; log "$1"; else warn "$1"; fi }
mkd() { if [ ! -d "$1" ]; then mkdir -p "$1"; log "$1/"; fi }

info "Root config..."
for f in .env.example .env .gitignore Makefile README.md pyproject.toml requirements.txt requirements-dev.txt docker-compose.yml Dockerfile .dockerignore; do mk "$f"; done

info "config/settings..."
for f in config/__init__.py config/asgi.py config/wsgi.py config/urls.py config/celery.py config/settings/__init__.py config/settings/base.py config/settings/development.py config/settings/production.py config/settings/testing.py; do mk "$f"; done

info "apps/crm..."
for f in apps/crm/__init__.py apps/crm/apps.py apps/crm/admin.py apps/crm/authentication.py apps/crm/decorators.py apps/crm/constants.py apps/crm/exceptions.py apps/crm/tasks.py apps/crm/models/__init__.py apps/crm/models/contact.py apps/crm/models/lead.py apps/crm/models/pipeline.py apps/crm/models/stage.py apps/crm/models/tag.py apps/crm/models/call.py apps/crm/models/note.py apps/crm/models/reminder.py apps/crm/models/permission.py apps/crm/models/signals.py apps/crm/serializers/__init__.py apps/crm/serializers/lead.py apps/crm/serializers/contact.py apps/crm/serializers/pipeline.py apps/crm/serializers/stage.py apps/crm/serializers/tag.py apps/crm/views/__init__.py apps/crm/views/lead_views.py apps/crm/views/contact_views.py apps/crm/views/pipeline_views.py apps/crm/views/dashboard.py apps/crm/urls/__init__.py apps/crm/urls/api_v1.py apps/crm/urls/web.py apps/crm/templatetags/__init__.py apps/crm/templatetags/crm_extras.py apps/crm/tests/__init__.py apps/crm/tests/test_models.py apps/crm/tests/test_views.py apps/crm/tests/test_serializers.py apps/crm/tests/test_services.py apps/crm/tests/factories.py apps/crm/migrations/__init__.py; do mk "$f"; done

info "apps/triggers..."
for f in apps/triggers/__init__.py apps/triggers/apps.py apps/triggers/admin.py apps/triggers/constants.py apps/triggers/tasks.py apps/triggers/models/__init__.py apps/triggers/models/trigger.py apps/triggers/serializers/__init__.py apps/triggers/serializers/trigger.py apps/triggers/views/__init__.py apps/triggers/views/trigger_views.py apps/triggers/urls/__init__.py apps/triggers/urls/api_v1.py apps/triggers/tests/__init__.py apps/triggers/tests/test_models.py apps/triggers/tests/test_tasks.py apps/triggers/tests/test_evaluator.py apps/triggers/tests/factories.py apps/triggers/migrations/__init__.py; do mk "$f"; done

info "apps/telephony (NEW)..."
for f in apps/telephony/__init__.py apps/telephony/apps.py apps/telephony/admin.py apps/telephony/constants.py apps/telephony/tasks.py apps/telephony/models/__init__.py apps/telephony/models/call_log.py apps/telephony/views/__init__.py apps/telephony/views/webhook_views.py apps/telephony/urls/__init__.py apps/telephony/urls/api_v1.py apps/telephony/tests/__init__.py apps/telephony/tests/test_webhooks.py apps/telephony/tests/test_tasks.py apps/telephony/migrations/__init__.py; do mk "$f"; done

info "apps/knowledge (NEW)..."
for f in apps/knowledge/__init__.py apps/knowledge/apps.py apps/knowledge/admin.py apps/knowledge/models/__init__.py apps/knowledge/models/document.py apps/knowledge/models/chunk.py apps/knowledge/serializers/__init__.py apps/knowledge/serializers/document.py apps/knowledge/views/__init__.py apps/knowledge/views/document_views.py apps/knowledge/urls/__init__.py apps/knowledge/urls/api_v1.py apps/knowledge/tasks.py apps/knowledge/tests/__init__.py apps/knowledge/tests/test_models.py apps/knowledge/tests/test_views.py apps/knowledge/migrations/__init__.py; do mk "$f"; done

info "services/..."
for f in services/__init__.py services/ai/__init__.py services/ai/copilot_service.py services/ai/embeddings_service.py services/ai/rag_service.py services/ai/prompts/__init__.py services/ai/prompts/crm_prompts.py services/ai/prompts/knowledge_prompts.py services/analytics/__init__.py services/analytics/report_service.py services/crm/__init__.py services/crm/lead_service.py services/crm/stage_service.py services/crm/contact_service.py services/knowledge/__init__.py services/knowledge/chunker_service.py services/knowledge/parser_service.py services/telephony/__init__.py services/telephony/scheduler_service.py services/telephony/tracker_service.py services/triggers/__init__.py services/triggers/evaluator.py services/triggers/actions.py services/notifications/__init__.py services/notifications/email_service.py services/notifications/push_service.py; do mk "$f"; done

info "core/..."
for f in core/__init__.py core/models.py core/permissions.py core/pagination.py core/exceptions.py core/middleware.py core/validators.py core/utils/__init__.py core/utils/datetime.py core/utils/strings.py core/utils/files.py; do mk "$f"; done

info "api/ router..."
for f in api/__init__.py api/v1/__init__.py api/v1/router.py; do mk "$f"; done

info "tests/..."
for f in tests/__init__.py tests/conftest.py tests/factories/__init__.py; do mk "$f"; done

info "static/ templates/ docs/ scripts/ ci..."
for d in static/css static/js static/images templates/crm templates/base scripts docs .github/workflows; do mkd "$d"; done
for f in templates/base/base.html templates/crm/dashboard.html scripts/seed_data.py scripts/backfill_embeddings.py docs/architecture.md docs/api.md docs/deployment.md docs/local_setup.md .github/workflows/ci.yml .github/workflows/deploy.yml; do mk "$f"; done

info "Cleanup..."
[ -f "apps/crm/views_legacy.py.bak" ] && rm "apps/crm/views_legacy.py.bak" && log "Removed views_legacy.py.bak"

echo ""
echo -e "${GREEN}Done! Next steps:${NC}"
echo "  1. python manage.py makemigrations triggers telephony knowledge"
echo "  2. Migrate content: apps/crm/serializers.py → apps/crm/serializers/"
echo "  3. Move services/telephony/webhooks.py → apps/telephony/views/webhook_views.py"
echo "  4. Register new apps in config/settings/base.py"
echo "  5. Wire api/v1/router.py in config/urls.py"

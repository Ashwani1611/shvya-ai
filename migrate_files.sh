#!/bin/bash
set -e
GREEN='\033[0;32m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[DONE]${NC} $1"; }
info() { echo -e "${BLUE}[...]${NC} $1"; }

# 1. crm/serializers.py → serializers/ (append content if target exists)
info "Migrating apps/crm/serializers.py..."
if [ -f apps/crm/serializers.py ]; then
  echo -e "\n# --- migrated from serializers.py ---" >> apps/crm/serializers/__init__.py
  cat apps/crm/serializers.py >> apps/crm/serializers/__init__.py
  rm apps/crm/serializers.py
  log "apps/crm/serializers.py merged into serializers/__init__.py"
fi

# 2. crm/urls.py → urls/api_v1.py
info "Migrating apps/crm/urls.py..."
if [ -f apps/crm/urls.py ]; then
  echo -e "\n# --- migrated from urls.py ---" >> apps/crm/urls/api_v1.py
  cat apps/crm/urls.py >> apps/crm/urls/api_v1.py
  rm apps/crm/urls.py
  log "apps/crm/urls.py merged into urls/api_v1.py"
fi

# 3. crm/web_urls.py → urls/web.py
info "Migrating apps/crm/web_urls.py..."
if [ -f apps/crm/web_urls.py ]; then
  echo -e "\n# --- migrated from web_urls.py ---" >> apps/crm/urls/web.py
  cat apps/crm/web_urls.py >> apps/crm/urls/web.py
  rm apps/crm/web_urls.py
  log "apps/crm/web_urls.py merged into urls/web.py"
fi

# 4. triggers/serializers.py → serializers/trigger.py
info "Migrating apps/triggers/serializers.py..."
if [ -f apps/triggers/serializers.py ]; then
  echo -e "\n# --- migrated from serializers.py ---" >> apps/triggers/serializers/trigger.py
  cat apps/triggers/serializers.py >> apps/triggers/serializers/trigger.py
  rm apps/triggers/serializers.py
  log "apps/triggers/serializers.py merged into serializers/trigger.py"
fi

# 5. triggers/views.py → views/trigger_views.py
info "Migrating apps/triggers/views.py..."
if [ -f apps/triggers/views.py ]; then
  echo -e "\n# --- migrated from views.py ---" >> apps/triggers/views/trigger_views.py
  cat apps/triggers/views.py >> apps/triggers/views/trigger_views.py
  rm apps/triggers/views.py
  log "apps/triggers/views.py merged into views/trigger_views.py"
fi

# 6. knowledge/serializers.py → serializers/document.py
info "Migrating apps/knowledge/serializers.py..."
if [ -f apps/knowledge/serializers.py ]; then
  echo -e "\n# --- migrated from serializers.py ---" >> apps/knowledge/serializers/document.py
  cat apps/knowledge/serializers.py >> apps/knowledge/serializers/document.py
  rm apps/knowledge/serializers.py
  log "apps/knowledge/serializers.py merged into serializers/document.py"
fi

# 7. knowledge/views.py → views/document_views.py
info "Migrating apps/knowledge/views.py..."
if [ -f apps/knowledge/views.py ]; then
  echo -e "\n# --- migrated from views.py ---" >> apps/knowledge/views/document_views.py
  cat apps/knowledge/views.py >> apps/knowledge/views/document_views.py
  rm apps/knowledge/views.py
  log "apps/knowledge/views.py merged into views/document_views.py"
fi

# 8. knowledge/models.py → models/__init__.py
info "Migrating apps/knowledge/models.py..."
if [ -f apps/knowledge/models.py ]; then
  echo -e "\n# --- migrated from models.py ---" >> apps/knowledge/models/__init__.py
  cat apps/knowledge/models.py >> apps/knowledge/models/__init__.py
  rm apps/knowledge/models.py
  log "apps/knowledge/models.py merged into models/__init__.py"
fi

# 9. services/telephony/webhooks.py → apps/telephony/views/webhook_views.py
info "Moving services/telephony/webhooks.py to apps/telephony/views/..."
if [ -f services/telephony/webhooks.py ]; then
  echo -e "\n# --- migrated from services/telephony/webhooks.py ---" >> apps/telephony/views/webhook_views.py
  cat services/telephony/webhooks.py >> apps/telephony/views/webhook_views.py
  rm services/telephony/webhooks.py
  log "services/telephony/webhooks.py moved to apps/telephony/views/webhook_views.py"
fi

# 10. services/ai/prompts.py → prompts/__init__.py
info "Migrating services/ai/prompts.py..."
if [ -f services/ai/prompts.py ]; then
  echo -e "\n# --- migrated from prompts.py ---" >> services/ai/prompts/__init__.py
  cat services/ai/prompts.py >> services/ai/prompts/__init__.py
  rm services/ai/prompts.py
  log "services/ai/prompts.py merged into prompts/__init__.py"
fi

# 11. Duplicate settings files
info "Removing duplicate settings files..."
[ -f config/settings/development.py ] && rm config/settings/development.py && log "Removed development.py (keep dev.py)"
[ -f config/settings/production.py ]  && rm config/settings/production.py  && log "Removed production.py (keep prod.py)"

echo ""
echo -e "${GREEN}All done! Now open each merged file in your editor and clean up the content.${NC}"
echo "Run: python manage.py check --deploy"

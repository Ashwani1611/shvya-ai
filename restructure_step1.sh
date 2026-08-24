#!/bin/bash
set -e

cd apps

APPS="accounts analytics calls channels copilot crm followups integrations knowledge organizations superadmin teams triggers"

for app in $APPS; do
  echo "== $app =="
  mkdir -p "$app/tests"
  touch "$app/tests/__init__.py"

  if [ -f "$app/tests.py" ]; then
    mv "$app/tests.py" "$app/tests/test_legacy.py"
  fi

  if [ -f "$app/views.py" ] && [ ! -f "$app/serializers.py" ] && [ ! -d "$app/serializers" ]; then
    touch "$app/serializers.py"
  fi
done

cd ..
echo "Step 1 done."

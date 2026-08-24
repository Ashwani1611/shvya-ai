#!/bin/bash
set -e

cd apps/triggers

if [ -f models.py ]; then
  mkdir -p models
  git mv models.py models/trigger.py 2>/dev/null || mv models.py models/trigger.py
  cat > models/__init__.py << 'PYEOF'
from .trigger import *  # noqa
PYEOF
  echo "triggers/models.py -> models/ package done."
else
  echo "models.py not found or already converted, skipping."
fi

cd ../..
echo "Step 2 done."

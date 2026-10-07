#!/usr/bin/env python3
"""Verify preserved source bytes without executing historical helpers."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'references/source-manifest.json').read_text())
failures=[]
for item in manifest['files']:
 path=root/item['path']
 if not path.is_file(): failures.append(item['path']+': missing'); continue
 data=path.read_bytes()
 if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']:
  failures.append(item['path']+': changed')
if failures:
 raise SystemExit('\n'.join(failures))
print(f"Verified {len(manifest['files'])} complete source files ({sum(i['bytes'] for i in manifest['files'])} bytes).")

"""Build a clean, reviewable code commit without moving any repository branch."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen

BASE = "e94ef0e566c0af105962042eda4f931f6cd2ed64"
REPO = "Ashwani1611/shvya-ai"
changes = {}


def original(path, sha):
    raw = subprocess.check_output(["git", "show", f"{BASE}:{path}"])
    actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    if actual != sha:
        raise RuntimeError(f"Inspected source changed: {path}")
    return raw.decode()


def function_replace(source, name, replacement, owner=None):
    tree = ast.parse(source)
    body = tree.body
    if owner:
        body = next(node for node in body if isinstance(node, ast.ClassDef) and node.name == owner).body
    matches = [node for node in body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(matches) != 1:
        raise RuntimeError(f"Function anchor mismatch: {name}")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [replacement.rstrip() + "\n"]
    output = "".join(lines)
    ast.parse(output)
    return output


path = "apps/ai_engagement/services/phase7_completion_runtime.py"
s = original(path, "fadd3e81cec23369a0d3519733fd92861b506864")
s = function_replace(s, "_deterministically_supported_reply", '''def _deterministically_supported_reply(decision, resolution) -> bool:
    from apps.ai_engagement.services.grounding_validation import exact_evidence_reply
    return exact_evidence_reply(decision, resolution)
''')
changes[path] = s
path = "apps/ai_engagement/services/phase5_6_safety_fixes.py"
s = original(path, "8efa560c0e9d7d964ab9e939a6534dc0a3aa04fc")
s = function_replace(s, "_extractive_evidence_match", '''def _extractive_evidence_match(decision, resolution) -> bool:
    from apps.ai_engagement.services.grounding_validation import exact_evidence_reply
    return exact_evidence_reply(decision, resolution, allow_price_wrapper=True)
''')
s = function_replace(s, "_low_risk_normal_reply", '''def _low_risk_normal_reply(decision, resolution) -> bool:
    from apps.ai_engagement.services.grounding_validation import factual_free_acknowledgement
    return factual_free_acknowledgement(decision, resolution)
''')
changes[path] = s
path = "apps/ai_engagement/services/crm_executor.py"
s = original(path, "e12b65ea673baa2e3bd40f3ac53eca4d6e19bbf7")
changes[path] = function_replace(s, "execute", Path(".phase_build/executor_method.txt").read_text(), owner="CRMActionExecutor")
path = "apps/ai_engagement/services/stage_transition_evidence.py"
s = original(path, "e68ad89235b709c937865700e1eca77905a89d53")
old = "    def execute(self, *, organization, lead, actions, actor=None):"
assert s.count(old) == 1
s = s.replace(old, "    def execute(self, *, organization, lead, actions, actor=None, source_message=None):")
old = "            actions=effective_actions,\n            actor=actor,\n"
assert s.count(old) == 1
s = s.replace(old, old + "            **({\"source_message\": source_message} if source_message is not None else {}),\n")
changes[path] = s
path = "apps/ai_engagement/models/__init__.py"
s = original(path, "1c6f6082942caeb5552b6cb77800c4b737a7b64c")
changes[path] = s + "from .reliability import AIActionReceipt as AIActionReceipt, LeadSignal as LeadSignal\n"

for path in [
    "apps/ai_engagement/services/grounding_validation.py",
    "apps/ai_engagement/models/reliability.py",
    "apps/ai_engagement/migrations/0016_action_receipts_lead_signals.py",
    "apps/ai_engagement/tests/test_phase_completion_safety.py",
]:
    changes[path] = Path(path).read_text()
for path, text in changes.items():
    if path.endswith(".py"):
        ast.parse(text, filename=path)
    Path(path).write_text(text)


def post(endpoint, data):
    request = Request(
        f"https://api.github.com/repos/{REPO}/{endpoint}",
        data=json.dumps(data).encode(), method="POST",
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


tree = post("git/trees", {
    "base_tree": "59df24412dd70017209a18c05fc6a3a4e7896640",
    "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in changes.items()],
})
commit = post("git/commits", {
    "message": "Repair AI grounding proofs, source propagation and transactional action idempotency",
    "tree": tree["sha"], "parents": [BASE],
})
print("CODE_COMMIT=" + commit["sha"])
print("CODE_TREE=" + tree["sha"])
for path in sorted(changes):
    print("CHANGED " + path)
with open(os.environ["GITHUB_OUTPUT"], "a") as output:
    output.write("commit=" + commit["sha"] + "\n")

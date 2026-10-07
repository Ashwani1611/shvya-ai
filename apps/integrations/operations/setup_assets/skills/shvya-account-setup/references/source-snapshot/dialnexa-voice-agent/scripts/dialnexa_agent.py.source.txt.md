#!/usr/bin/env python3
"""Tiny Dialnexa helper. Key from $DIALNEXA_KEY or --key-file.

  dialnexa_agent.py show <agent_id>                  versions, live flag, settings
  dialnexa_agent.py prompt <agent_id> [version]      print prompt_text (default: live)
  dialnexa_agent.py patch <agent_id> <version> <json-file>   PATCH fields onto a version, then re-read
  dialnexa_agent.py publish <agent_id> <version>
  dialnexa_agent.py calls [limit]
  dialnexa_agent.py call <call_id>                   meta + post-call analysis (transcript is in Kraya's dialnexa_webhook_payloads)
"""
import json, os, subprocess, sys

BASE = "https://api.dialnexa.com/v1"


def key():
    k = os.environ.get("DIALNEXA_KEY")
    if not k and "--key-file" in sys.argv:
        k = open(sys.argv[sys.argv.index("--key-file") + 1]).read().strip()
    if not k:
        sys.exit("set DIALNEXA_KEY or pass --key-file <path>")
    return k


def req(method, path, body=None):
    # curl instead of urllib: python.org builds on macOS ship without a CA bundle.
    cmd = ["curl", "-s", "-m", "30", "-X", method, "-H", "Authorization: Bearer " + key(),
           "-H", "Content-Type: application/json", BASE + path]
    if body is not None:
        cmd += ["--data-binary", json.dumps(body, ensure_ascii=False)]
    out = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
    if isinstance(out, dict) and out.get("error"):
        sys.exit(f"Dialnexa {out.get('statusCode')}: {out.get('message')} {out.get('errors')}")
    return out.get("data", out) if isinstance(out, dict) else out


def agent(aid):
    return req("GET", f"/agents/{aid}")


def live(a):
    return next(v for v in a["versions"] if v.get("is_published"))


def show(aid):
    a = agent(aid)
    print(f"{a['id']}  {a.get('pipeline_type')}  tz={a.get('timezone')}  draft=v{a['current_version_number']}")
    for v in a["versions"][:6]:
        pca = [x["field_name"] for x in v.get("postcall_analysis") or []]
        print(f"  v{v['version_number']:>3} {'LIVE' if v.get('is_published') else '    '} llm={v.get('llm_id')} temp={v.get('llm_temperature')} "
              f"speed={v.get('voice_speed')} fallback={v.get('fallback_llm_enabled')} max={v.get('max_call_duration_sec')}s "
              f"prompt={len(v.get('prompt_text') or '')}ch pca={pca[:3]}")
    print("  welcome:", live(a).get("welcome_message"))


def cmd_prompt(aid, ver=None):
    a = agent(aid)
    v = live(a) if ver is None else next(v for v in a["versions"] if v["version_number"] == str(ver))
    print(v.get("prompt_text") or "")


def patch(aid, ver, path):
    body = json.load(open(path))
    body["version_number"] = int(ver)
    req("PATCH", f"/agents/{aid}", body)
    a = agent(aid)
    v = next(v for v in a["versions"] if v["version_number"] == str(ver))
    for k in body:
        if k == "version_number":
            continue
        ok = v.get(k) == body[k] if k != "postcall_analysis" else [x["field_name"] for x in v.get(k) or []] == [x["field_name"] for x in body[k]]
        print(f"  {k}: {'ok' if ok else 'NOT APPLIED'}")


def publish(aid, ver):
    req("PATCH", f"/agents/{aid}", {"version_number": int(ver), "is_published": True})
    a = agent(aid)
    print("published:", [v["version_number"] for v in a["versions"] if v.get("is_published")][:3], "draft: v" + a["current_version_number"])


def calls(limit=10):
    for c in req("GET", f"/calls?limit={limit}"):
        print(c["id"], c.get("called_time"), c.get("status"), f"{c.get('duration')}s", c.get("to_number"), c.get("llm_name"), c.get("end_reason"))


def call(cid):
    c = req("GET", f"/calls/{cid}")
    for k in ("agent_id", "called_time", "duration", "status", "end_reason", "llm_name", "voice_model_name", "to_number"):
        print(f"{k}: {c.get(k)}")
    print("post-call:", json.dumps(c.get("postcallanalysis"), ensure_ascii=False))
    print("recording:", (c.get("recording_sas_url") or "")[:80] + "...")
    print("transcript: query dialnexa_webhook_payloads (see references/dialnexa-api.md)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--key-file" in sys.argv:
        i = sys.argv.index("--key-file"); args = [a for a in args if a != sys.argv[i + 1]]
    if not args:
        sys.exit(__doc__)
    c, rest = args[0], args[1:]
    {"show": show, "prompt": cmd_prompt, "patch": patch, "publish": publish, "calls": calls, "call": call}[c](*rest)

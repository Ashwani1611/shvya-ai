#!/usr/bin/env python3
"""Kraya Vault agent API helper (stdlib only).

Usage (token passed inline, never stored):
  VAULT_TOKEN=kv_... python3 vault.py status
  VAULT_TOKEN=kv_... python3 vault.py pull [--out DIR]
  VAULT_TOKEN=kv_... python3 vault.py note --section basics --origin fireflies \
      --date 2026-09-12 --external-id ff_abc:basics --body "One branch, Pune. Mon-Sat 10-7."
  VAULT_TOKEN=kv_... python3 vault.py note --section faqs --origin whatsapp --file facts.txt
  VAULT_TOKEN=kv_... python3 vault.py ask --section offerings --text "What is the minimum order quantity?" \
      --external-id ff_abc:q1
  VAULT_TOKEN=kv_... python3 vault.py call --title "Brainstorming call" --date 2026-09-16 \
      --url https://app.fireflies.ai/view/abc --duration 38 --attendee "Nishtha (Kraya)" --attendee "Pooja Nair" \
      --external-id ff_abc --summary "Agreed the qualification questions and the pricing rule."

Env: VAULT_TOKEN (required), VAULT_URL (default https://vault.kraya-ai.com).
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path


def ssl_context() -> ssl.SSLContext:
    """System CA bundle, or certifi's when the interpreter ships without one (python.org builds on macOS)."""
    ctx = ssl.create_default_context()
    try:
        import certifi  # type: ignore

        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


SSL_CTX = ssl_context()

SECTIONS = [
    "website", "brochures", "media", "offerings", "basics", "faqs", "team",
    "qualification", "handoff", "blacklist", "rules", "proof", "offers", "scripts", "other",
]
ORIGINS = ["fireflies", "whatsapp", "ops_chat", "other"]


def base_url() -> str:
    return os.environ.get("VAULT_URL", "https://vault.kraya-ai.com").rstrip("/")


def token() -> str:
    tok = os.environ.get("VAULT_TOKEN", "").strip()
    if not tok.startswith("kv_"):
        sys.exit("VAULT_TOKEN is missing. Ask the rep to paste the client's Vault agent token and pass it inline.")
    return tok


def call(method: str, path: str, body: dict | None = None, raw: bool = False):
    req = urllib.request.Request(
        f"{base_url()}/api/agent{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as res:
            data = res.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        sys.exit(f"Vault {e.code} on {method} {path}: {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Vault unreachable ({base_url()}): {e.reason}")
    return data.decode() if raw else json.loads(data)


def download(url: str, dest: Path, size: int | None) -> bool:
    """Fetches a signed URL into dest; skips when a same-size copy exists. Returns True when written."""
    if dest.exists() and size is not None and dest.stat().st_size == size:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300, context=SSL_CTX) as res, open(dest, "wb") as fh:
        while chunk := res.read(1 << 20):
            fh.write(chunk)
    return True


def cmd_status(_: argparse.Namespace) -> None:
    ws = call("GET", "/workspace")
    print(f"{ws['name']} — {ws['status']}" + (f" on {ws['submitted_at'][:10]}" if ws.get("submitted_at") else ""))
    for s in ws["sections"]:
        mark = {"filled": "✓", "dont_have": "–", "empty": " "}[s["state"]]
        print(f"  [{mark}] {s['key']:<14} {s['title']} ({s['entries']})")
    open_q = [e for e in ws["entries"] if e["kind"] == "question" and not e["answered_at"]]
    print(f"  open questions: {len(open_q)}")


def cmd_pull(args: argparse.Namespace) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "export.md").write_text(call("GET", "/export.md", raw=True))
    ws = call("GET", "/workspace")
    (out / "manifest.json").write_text(json.dumps(ws, indent=2))
    fetched = skipped = 0
    for e in ws["entries"]:
        for url, name in ((e.get("download_url"), e.get("file_name")), (e.get("audio_url"), f"dictation-{e['id'][:8]}.audio")):
            if not url:
                continue
            dest = out / "files" / e["section"] / (name or e["id"])
            if download(url, dest, e.get("size") if name == e.get("file_name") else None):
                fetched += 1
            else:
                skipped += 1
    print(f"pulled {ws['name']}: export.md, manifest.json, {fetched} file(s) downloaded, {skipped} already present → {out}")


def read_body(args: argparse.Namespace) -> str:
    if args.body:
        return args.body
    if args.file:
        return Path(args.file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    sys.exit("Provide --body, --file, or pipe text on stdin.")


def cmd_note(args: argparse.Namespace) -> None:
    payload = {"section": args.section, "origin": args.origin, "body": read_body(args).strip()}
    if args.date:
        payload["source_date"] = args.date
    if args.external_id:
        payload["external_id"] = args.external_id
    res = call("POST", "/entries", payload)
    print(f"{'updated' if res.get('updated') else 'created'} note {res['id']} in {args.section}")


def cmd_call(args: argparse.Namespace) -> None:
    payload = {"title": args.title, "date": args.date}
    if args.url:
        payload["url"] = args.url
    if args.duration:
        payload["duration_min"] = args.duration
    if args.attendee:
        payload["attendees"] = args.attendee
    if args.summary or args.summary_file:
        payload["summary"] = args.summary or Path(args.summary_file).read_text()
    if args.external_id:
        payload["external_id"] = args.external_id
    if args.hide_recording:
        payload["share_recording"] = False
    res = call("POST", "/calls", payload)
    print(f"{'updated' if res.get('updated') else 'recorded'} call {res['id']} ({args.date})")


def cmd_ask(args: argparse.Namespace) -> None:
    payload = {"section": args.section, "text": args.text}
    if args.external_id:
        payload["external_id"] = args.external_id
    res = call("POST", "/questions", payload)
    print(f"posted question {res['id']} in {args.section}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    pull = sub.add_parser("pull")
    pull.add_argument("--out", default="vault")
    pull.set_defaults(fn=cmd_pull)
    note = sub.add_parser("note")
    note.add_argument("--section", required=True, choices=SECTIONS)
    note.add_argument("--origin", required=True, choices=ORIGINS)
    note.add_argument("--date", help="YYYY-MM-DD the fact was said (call date)")
    note.add_argument("--external-id", help="stable id so re-runs update instead of duplicating")
    note.add_argument("--body")
    note.add_argument("--file")
    note.set_defaults(fn=cmd_note)
    c = sub.add_parser("call")
    c.add_argument("--title", required=True)
    c.add_argument("--date", required=True, help="YYYY-MM-DD the call happened")
    c.add_argument("--url", help="Fireflies (or other) recording link")
    c.add_argument("--duration", type=int, help="minutes")
    c.add_argument("--attendee", action="append", help="repeat per attendee")
    c.add_argument("--summary")
    c.add_argument("--summary-file")
    c.add_argument("--external-id", help="the Fireflies transcript id, so re-runs update")
    c.add_argument("--hide-recording", action="store_true", help="keep the recording link from the client (shared by default)")
    c.set_defaults(fn=cmd_call)
    ask = sub.add_parser("ask")
    ask.add_argument("--section", default="other", choices=SECTIONS)
    ask.add_argument("--text", required=True)
    ask.add_argument("--external-id")
    ask.set_defaults(fn=cmd_ask)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read a WhatsApp group (or chat) through a Kraya ops hosted WAHA session.

Read-only. Resolves the session by phone / owner name / public_id, finds the
chat by name, pages its messages, and prints a chronological IST transcript.

Auth: KRAYA_OPS_ACCOUNT_TOKEN (bearer). Base: KRAYA_API_BASE_URL
(default https://api.kraya-ai.com/api). Transport is curl.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
READABLE_STATUSES = {"running", "syncing"}
PAGE = 100


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def get(base: str, token: str, path: str) -> dict:
    """GET a Kraya API path with curl and return the decoded JSON body."""
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", "90", f"{base}{path}",
         "-H", f"Authorization: Bearer {token}", "-H", "Accept: application/json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        die(f"curl failed for {path}: {proc.stderr.strip()}")
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        die(f"Non-JSON response for {path}: {proc.stdout[:300]}")
    if isinstance(body, dict) and body.get("success") is False:
        die(f"API error for {path}: {body.get('message')}")
    if isinstance(body, dict) and "message" in body and "success" not in body and not any(k in body for k in ("sessions", "data")):
        die(f"API error for {path}: {body.get('message')} (check the token)")
    return body


def digits(value: str) -> str:
    d = re.sub(r"\D", "", value or "")
    return "91" + d if len(d) == 10 else d


def pick_session(sessions: list, phone: str, owner: str, public_id: str) -> list:
    """Return the sessions matching the selector (all readable sessions if none given)."""
    if public_id:
        return [s for s in sessions if s.get("public_id") == public_id]
    if phone:
        want = digits(phone)
        return [s for s in sessions if digits(s.get("phone_number", "")) == want]
    if owner:
        return [s for s in sessions if owner.lower() in (s.get("owner_name") or "").lower()]
    return [s for s in sessions if s.get("status") in READABLE_STATUSES]


def find_chat(base: str, token: str, session_id: str, name: str) -> list:
    """Chats on the session whose name matches; exact (case-insensitive) match preferred."""
    q = urllib.parse.quote(name)
    rows = get(base, token, f"/waha/sessions/{session_id}/chats?q={q}&limit=50").get("data", [])
    exact = [c for c in rows if (c.get("name") or "").strip().lower() == name.strip().lower()]
    return exact or rows


def fetch_messages(base: str, token: str, session_id: str, chat_id: str, limit: int, since: datetime, until: datetime) -> list:
    """Page newest-first messages until `limit` or the `since` bound is passed."""
    out, offset = [], 0
    while len(out) < limit:
        page = get(base, token, f"/waha/sessions/{session_id}/chats/{urllib.parse.quote(chat_id)}/messages?limit={PAGE}&offset={offset}").get("data", [])
        if not page:
            break
        for m in page:
            ts = datetime.fromtimestamp(int(m.get("timestamp") or 0), IST)
            if until and ts > until:
                continue
            if since and ts < since:
                return out
            out.append(m)
            if len(out) >= limit:
                break
        if len(page) < PAGE:
            break
        offset += PAGE
    return out


def render(messages: list, self_label: str) -> str:
    lines = []
    for m in sorted(messages, key=lambda x: int(x.get("timestamp") or 0)):
        ts = datetime.fromtimestamp(int(m.get("timestamp") or 0), IST).strftime("%d %b %H:%M")
        sender = self_label if m.get("fromMe") else (m.get("senderName") or m.get("participantName") or m.get("from") or "?")
        text = (m.get("body") or m.get("caption") or "").strip()
        tag = ""
        if m.get("hasMedia") or (m.get("type") not in (None, "text", "chat")):
            media = m.get("media") or {}
            tag = f" [{m.get('type') or 'media'}{': ' + media['filename'] if media.get('filename') else ''}]"
        reply = " (reply)" if m.get("replyTo") else ""
        lines.append(f"[{ts}] {sender}:{reply} {text}{tag}".rstrip())
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read a WhatsApp group via a Kraya ops WAHA session (read-only).")
    ap.add_argument("--group", help="group or chat name (substring ok); or a phone number for a 1:1 chat")
    ap.add_argument("--phone", help="ops number the group is on (10 digits → 91 prefixed)")
    ap.add_argument("--owner", help="ops team member's name (matches owner_name)")
    ap.add_argument("--session", help="WAHA session public_id")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--since", help="YYYY-MM-DD (IST), inclusive")
    ap.add_argument("--until", help="YYYY-MM-DD (IST), inclusive")
    ap.add_argument("--json", action="store_true", help="print raw messages instead of a transcript")
    ap.add_argument("--list-sessions", action="store_true")
    ap.add_argument("--list-groups", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("KRAYA_OPS_ACCOUNT_TOKEN")
    if not token:
        die("KRAYA_OPS_ACCOUNT_TOKEN is not set")
    base = os.environ.get("KRAYA_API_BASE_URL", "https://api.kraya-ai.com/api").rstrip("/")

    sessions = get(base, token, "/waha/sessions").get("sessions", [])
    if args.list_sessions:
        for s in sessions:
            print(f"{s.get('public_id')}\t{s.get('display_phone_number')}\t{s.get('status')}\t{s.get('owner_name')}")
        return

    candidates = pick_session(sessions, args.phone, args.owner, args.session)
    if not candidates:
        die("No hosted session matches that phone / owner / session id. Use --list-sessions.")
    unreadable = [s for s in candidates if s.get("status") not in READABLE_STATUSES]
    candidates = [s for s in candidates if s.get("status") in READABLE_STATUSES]
    if not candidates:
        s = unreadable[0]
        die(f"Session for {s.get('display_phone_number')} ({s.get('owner_name')}) is '{s.get('status')}', not connected; nothing can be read.")

    if args.list_groups:
        for s in candidates:
            rows = get(base, token, f"/waha/sessions/{s['public_id']}/chats?limit=200").get("data", [])
            for c in rows:
                if str(c.get("id", "")).endswith("@g.us"):
                    print(f"{s.get('owner_name')}\t{c.get('id')}\t{c.get('name')}")
        return

    if not args.group:
        die("--group is required (or use --list-sessions / --list-groups)")

    # A bare phone number means a 1:1 chat: build the chat id directly.
    if re.fullmatch(r"[+\d\s-]{8,}", args.group):
        matches = [(s, {"id": digits(args.group) + "@c.us", "name": args.group}) for s in candidates]
    else:
        matches = [(s, c) for s in candidates for c in find_chat(base, token, s["public_id"], args.group)]
    if not matches:
        die(f"No chat named like '{args.group}' on {', '.join(s.get('owner_name') or s.get('display_phone_number') for s in candidates)}.")
    if len(matches) > 1:
        print("Ambiguous, pick one and rerun with --session and the exact --group:", file=sys.stderr)
        for s, c in matches:
            print(f"  --session {s['public_id']} ({s.get('owner_name')}, {s.get('display_phone_number')})  chat: {c.get('name')}  id: {c.get('id')}", file=sys.stderr)
        sys.exit(2)

    session, chat = matches[0]
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=IST) if args.since else None
    until = (datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=IST) + timedelta(days=1)) if args.until else None
    messages = fetch_messages(base, token, session["public_id"], chat["id"], args.limit, since, until)

    if args.json:
        print(json.dumps(messages, ensure_ascii=False, indent=1))
        return
    header = f"# {chat.get('name')}  ({chat.get('id')})  via {session.get('owner_name')} {session.get('display_phone_number')}  |  {len(messages)} messages, oldest first, IST"
    print(header)
    print(render(messages, self_label=f"{session.get('owner_name')} (ops)"))


if __name__ == "__main__":
    main()

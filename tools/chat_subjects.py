#!/usr/bin/env python3
"""Give web-chat tickets a subject that says what the chat is about.

Zendesk names every messaging ticket "Conversation with Web User 6ae4…",
which tells the team nothing. Triggers can't change a subject, so this
script reads the chat transcript (it lands on the ticket when the chat is
handed off or ends) and rewrites the subject from what the customer said:

    Damaged item · #29497 · Jackie1stuart@sbcglobal.net · chargeback threat
    Return labels · #28935 · farina43537@gmail.com
    Product question · Streamline · glare

Only tickets still carrying the default "Conversation with …" subject are
touched, so a subject an agent has edited is never overwritten, and a ticket
is renamed once.

Usage:
  python3 tools/chat_subjects.py              # dry run: print proposed subjects
  python3 tools/chat_subjects.py --apply      # write them to Zendesk
  python3 tools/chat_subjects.py --days 7     # look further back (default 3)

Needs ZENDESK_API (an API token for anthony@americanflat.com).
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from cx_complaint_signals import (  # noqa: E402  (shared keyword lists)
    BULK, DAMAGE, DEFECT, FAMILIES, FAMILY_ALIASES, LATE, MISSING, OOS, TOPICS, WRONG,
)

BASE = "https://americanflat.zendesk.com/api/v2"
DEFAULT_SUBJECT = re.compile(r"^Conversation with ", re.I)
LINE = re.compile(r"^\((\d{1,2}:\d{2}:\d{2}\s?[AP]M)\)\s+([^:]+?):\s?(.*)$")
STAFF = re.compile(r"Americanflat\b|Automated Response", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ORDER = re.compile(r"(?<![\d.$-])((?:2|3)\d{4})(?![\d-])")
NAME = re.compile(r"(?i:\bmy name is)\s+([A-Za-z][A-Za-z'-]+(?:\s+[A-Za-z][A-Za-z'-]+)?)")
MAX_LEN = 120

# First match wins, so order matters: the most specific / most urgent first.
TOPIC_RULES = [
    ("Cancel order", re.compile(r"\bcancel", re.I)),
    ("Address change", re.compile(r"(change|wrong|update)\s+(the\s+|my\s+)?(shipping\s+)?address", re.I)),
    ("Wrong item", WRONG),
    ("Damaged item", re.compile(f"(?:{DAMAGE.pattern})|(?:{DEFECT.pattern})", re.I)),
    ("Missing item", MISSING),
    ("Return labels", re.compile(r"(return|shipping|mailing)\s+label", re.I)),
    ("Return", re.compile(r"\breturn", re.I)),
    ("Refund", re.compile(r"\brefund", re.I)),
    ("Where is my order", LATE),
    ("Bulk / wholesale", BULK),
    ("Back in stock", OOS),
    ("Discount code", re.compile(r"discount|promo|coupon|\bcode\b", re.I)),
]
PERSON = re.compile(r"\bhuman\b|real person|\bagent\b|representative|speak to someone", re.I)
FLAGS = [
    ("chargeback threat", re.compile(r"chargeback|dispute|credit card company|\bbbb\b|better business", re.I)),
    ("deadline", re.compile(r"\basap\b|urgent|deadline|wedding|funeral|art show|this weekend|next weekend|by tomorrow", re.I)),
    ("contacted before", re.compile(r"(still|already)\s+(not\s+)?(heard|sent|emailed|contacted|called)|no one is respond|no response|haven'?t heard", re.I)),
]


def api(method, path, body=None):
    token = os.environ.get("ZENDESK_API")
    if not token:
        sys.exit("ZENDESK_API is not set")
    auth = base64.b64encode(f"anthony@americanflat.com/token:{token}".encode()).decode()
    url = path if path.startswith("http") else BASE + path
    for _ in range(6):
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", 10)))
                continue
            return {"_error": e.code, "_body": e.read().decode()[:300]}
    return {"_error": 429}


def candidates(days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    q = f'type:ticket via:native_messaging status<solved created>{since} subject:"Conversation with"'
    url = "/search.json?query=" + urllib.request.quote(q) + "&per_page=100"
    out = []
    while url:
        d = api("GET", url)
        if "_error" in d:
            sys.exit(f"Zendesk search failed: {d}")
        out += [t for t in d["results"] if DEFAULT_SUBJECT.match(t.get("subject") or "")]
        url = d.get("next_page")
    return out


def customer_lines(comments):
    """The customer's own messages from the transcript comment(s)."""
    said = []
    for c in comments:
        if not c.get("public"):
            continue
        current = None
        for raw in (c.get("plain_body") or c.get("body") or "").splitlines():
            m = LINE.match(raw.strip())
            if m:
                who, text = m.group(2), m.group(3)
                current = None if STAFF.search(who) else who
                if current and not text.startswith("uploaded:"):
                    said.append(text)
            elif current and raw.strip() and not raw.startswith(("URL:", "Type:", "Size:")):
                said.append(raw.strip())
    return said


def family_of(text):
    low = text.lower()
    for f in FAMILIES:
        if re.search(r"\b" + re.escape(f.lower()) + r"\b", low):
            return f
    for alias, f in FAMILY_ALIASES.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            return f
    return None


def subject_for(ticket, comments, requester):
    said = customer_lines(comments)
    if not said:
        return None  # no transcript yet: the chat is still with the bot
    text = "\n".join(said)

    topic = next((name for name, pat in TOPIC_RULES if pat.search(text)), None)
    fam = family_of(text)
    if not topic:
        q = next((t for t, pat in TOPICS if pat.search(text)), None)
        topic = "Product question" if (q or fam) else None
    if not topic and PERSON.search(text):
        topic = "Asked for a person"
    if not topic:
        first = re.sub(r"\s+", " ", said[0]).strip(" .?!")
        topic = first[:50] + ("…" if len(first) > 50 else "")

    parts = [topic]
    if topic == "Product question":
        q = next((t for t, pat in TOPICS if pat.search(text)), None)
        parts += [p for p in (fam, q.split(" /")[0].lower() if q else None) if p]
    order = ORDER.search(text)
    if order:
        parts.append("#" + order.group(1))
    name = requester.get("name") or ""
    if not name or name.startswith("Web User"):
        m = NAME.search(text)
        name = m.group(1).title() if m else ""
    found = EMAIL.findall(text)
    email = requester.get("email") or (found[-1] if found else "")
    parts += [name, email.lower()]
    parts += [flag for flag, pat in FLAGS if pat.search(text)]
    subject = " · ".join(dict.fromkeys(p for p in parts if p))
    return subject[:MAX_LEN]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the new subjects (default: dry run)")
    ap.add_argument("--days", type=int, default=3, help="look at chats created in the last N days")
    a = ap.parse_args()

    proposed = renamed = waiting = failed = 0
    for t in candidates(a.days):
        comments = api("GET", f"/tickets/{t['id']}/comments.json").get("comments", [])
        requester = api("GET", f"/users/{t['requester_id']}.json").get("user", {})
        new = subject_for(t, comments, requester)
        if not new:
            waiting += 1
            continue
        proposed += 1
        print(f"#{t['id']}: {new}")
        if not a.apply:
            continue
        r = api("PUT", f"/tickets/{t['id']}.json",
                {"ticket": {"subject": new, "safe_update": True, "updated_stamp": t["updated_at"]}})
        if "_error" in r:
            failed += 1
            print(f"  not renamed ({r['_error']}): {r.get('_body', '')}")
        else:
            renamed += 1
    if a.apply:
        print(f"renamed {renamed}, failed {failed}, {waiting} still waiting for a transcript")
    else:
        print(f"dry run: would rename {proposed}, {waiting} still waiting for a transcript")


if __name__ == "__main__":
    main()

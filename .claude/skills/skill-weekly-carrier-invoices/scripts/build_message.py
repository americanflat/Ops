#!/usr/bin/env python3
"""Build the weekly carrier-invoice message for AP from summary.json.

Usage: python3 scripts/build_message.py <summary.json> [--ups-credit-note] [--with-notes]

  --ups-credit-note  add a line under UPS saying the billed packages are being credited back by Yusen
  --with-notes       append the processing flags (internal; off by default)

Output is standard markdown (what slack_send_message_draft takes). The layout is the one
Anthony approved on 2026-10-27; keep it unless he asks for a change.
"""
import argparse, json, os
from datetime import date
from decimal import Decimal

# Escaped so the leading "**" shows literally instead of opening bold.
STAMPS_NOTICE = (r"\*\*No action is needed for you on the Stamps.com amounts/invoices. This just serves as "
                 "a notice for you to tie the Auctane invoices to. If this isn't needed, let me know and "
                 "I'll take it off.")

def money(v): return f"${Decimal(str(v)):,.2f}"
def md(v):  # "2026-10-26" -> "10/26"
    try: d = date.fromisoformat(str(v)); return f"{d.month}/{d.day}"
    except ValueError: return str(v)
def rng(d):
    return f"{md(d['ship_from'])} to {md(d['ship_to'])}" if d.get("ship_from") else "n/a"

ap = argparse.ArgumentParser(); ap.add_argument("summary")
ap.add_argument("--ups-credit-note", action="store_true"); ap.add_argument("--with-notes", action="store_true")
a = ap.parse_args()
s = json.load(open(a.summary))
cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")))
# Address people with a Slack mention of their member ID, never a typed name.
who = f"<@{cfg['ap_recipient']['slack_user_id']}>"
lines = [f"Hi {who}, here are this week's carrier invoices:", ""]
grand = Decimal("0")
if "fedex" in s:
    f = s["fedex"]; grand += Decimal(f["total"])
    lines.append(f"**FedEx** ({f['packages']:,} packages, shipped {rng(f)})")
    for k, v in f["invoices"].items():
        lines.append(f"• Invoice {k} ({md(v['invoice_date'])}): {money(v['amount'])}")
    lines.append(f"FedEx total: **{money(f['total'])}**"); lines.append("")
if "ups" in s:
    u = s["ups"]; grand += Decimal(u["total"])
    lines.append(f"**UPS** ({u['paid_packages']} billed packages; {u['third_party_zero']:,} third-party at $0)")
    for k, v in u["invoices"].items():
        lines.append(f"• Invoice {k} ({md(v['invoice_date'])}): {money(v['amount'])}")
    note = (f". Note: all {u['paid_packages']} billed packages are South Carolina shipments that should have "
            "been billed third-party. We've requested a credit memo from Yusen for the full amount."
            if a.ups_credit_note and u["paid_packages"] else "")
    lines.append(f"UPS total: **{money(u['total'])}**{note}"); lines.append("")
if "stamps" in s:
    st = s["stamps"]; grand += Decimal(st["total"])
    lines.append(f"**Stamps.com** (account {', '.join(st['accounts'])}, {st['labels']:,} labels, shipped {rng(st)})")
    for k, v in st["by_carrier"].items():
        lines.append(f"• {k}: {v['labels']:,} labels, {money(v['amount'])}")
    lines.append(f"Stamps total: **{money(st['total'])}** (prepaid postage)"); lines.append("")
    lines.append(STAMPS_NOTICE); lines.append("")
lines.append(f"**Week total: {money(grand)}**")
notes = [x for c in ("fedex", "ups", "stamps") for x in s.get(c, {}).get("flags", [])]
if notes and a.with_notes:
    lines += ["", "Notes:"] + [f"• {n}" for n in notes]
if s.get("missing_carriers"):
    lines += ["", f"Not included this week: {', '.join(m.title() for m in s['missing_carriers'])}"]
print("\n".join(lines))

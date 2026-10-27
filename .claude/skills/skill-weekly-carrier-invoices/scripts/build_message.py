#!/usr/bin/env python3
"""Build the weekly carrier-invoice message for AP from summary.json.

Usage: python3 scripts/build_message.py <summary.json>  -> prints Slack-markdown message
"""
import json, sys
from decimal import Decimal

def money(v): return f"${Decimal(str(v)):,.2f}"
def rng(d):
    return f"{d.get('ship_from')} to {d.get('ship_to')}" if d.get("ship_from") else "n/a"

s = json.load(open(sys.argv[1]))
lines = ["Hi Georgina, here are this week's carrier invoices:", ""]
grand = Decimal("0")
if "fedex" in s:
    f = s["fedex"]; grand += Decimal(f["total"])
    lines.append(f"*FedEx* ({f['packages']:,} packages, shipped {rng(f)})")
    for k, v in f["invoices"].items():
        lines.append(f"• Invoice {k} ({v['invoice_date']}): {money(v['amount'])}")
    lines.append(f"FedEx total: *{money(f['total'])}*"); lines.append("")
if "ups" in s:
    u = s["ups"]; grand += Decimal(u["total"])
    lines.append(f"*UPS* ({u['paid_packages']} billed packages; {u['third_party_zero']:,} third-party at $0)")
    for k, v in u["invoices"].items():
        lines.append(f"• Invoice {k} ({v['invoice_date']}): {money(v['amount'])}")
    lines.append(f"UPS total: *{money(u['total'])}*"); lines.append("")
if "stamps" in s:
    st = s["stamps"]; grand += Decimal(st["total"])
    lines.append(f"*Stamps.com* (account {', '.join(st['accounts'])}, {st['labels']:,} labels, shipped {rng(st)})")
    for k, v in st["by_carrier"].items():
        lines.append(f"• {k}: {v['labels']:,} labels, {money(v['amount'])}")
    lines.append(f"Stamps total: *{money(st['total'])}* (prepaid postage)"); lines.append("")
lines.append(f"*Week total: {money(grand)}*")
notes = [x for c in ("fedex", "ups", "stamps") for x in s.get(c, {}).get("flags", [])]
if notes:
    lines += ["", "Notes:"] + [f"• {n}" for n in notes]
if s.get("missing_carriers"):
    lines += ["", f"Not included this week: {', '.join(m.title() for m in s['missing_carriers'])}"]
print("\n".join(lines))

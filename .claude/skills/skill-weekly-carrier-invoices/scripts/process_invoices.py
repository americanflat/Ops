#!/usr/bin/env python3
"""Process a week's carrier invoice files (FedEx, UPS, Stamps.com) into append files + a summary.

Usage:
    python3 scripts/process_invoices.py <file-or-folder> [...] --out <output_dir>

Auto-detects each file:
  FedEx    : CSV whose header has "Express or Ground Tracking ID" and "Net Charge Amount"
  UPS      : headerless UPS billing data CSV whose first field is "2.1"
  Stamps   : Print History CSV whose header starts with "Date Printed"
Writes (only for carriers present):
  fedex_append_<date>.csv, stamps_append_<date>.csv, ups_append_<date>.csv, summary.json
Stdlib only.
"""
import argparse, csv, json, os, re, sys
from collections import defaultdict, Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation

def D(v):
    try: return Decimal(str(v or "0").replace(",", "").replace("$", "").strip() or "0")
    except InvalidOperation: return Decimal("0")

def base(p):  # drop the 8-hex prefix chat uploads add, e.g. "70fc578d-FedEx_invoice_...CSV"
    return re.sub(r"^[0-9a-f]{8}-", "", os.path.basename(p))

def clean_excel(v):  # Stamps exports wrap values as ="..."
    return re.sub(r'^="?|"$', "", (v or "")).replace('"', "").strip()

def detect(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        first = next(csv.reader(fh), [])
    if first and first[0].strip() == "2.1": return "ups"
    if "Express or Ground Tracking ID" in first and "Net Charge Amount" in first: return "fedex"
    if first and first[0].strip() == "Date Printed": return "stamps"
    return None

def iso(v, fmts=("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y")):
    for f in fmts:
        try: return datetime.strptime(v.strip(), f).date().isoformat()
        except (ValueError, AttributeError): pass
    return v

def process_fedex(files, out, stamp, skip=()):
    rows, inv = [], defaultdict(lambda: {"rows": 0, "amount": Decimal("0"), "invoice_date": ""})
    seen, dup_rows, skipped = set(), 0, Counter()
    for p in files:
        for r in csv.DictReader(open(p, newline="", encoding="utf-8-sig")):
            if r["Invoice Number"] in skip:  # already loaded / already sent to AP
                skipped[r["Invoice Number"]] += 1; continue
            key = (r["Invoice Number"], r["Express or Ground Tracking ID"].strip())
            if key in seen:  # same invoice downloaded twice, e.g. "file (1).CSV"
                dup_rows += 1; continue
            seen.add(key)
            amt = D(r["Net Charge Amount"])
            rows.append({"Invoice Date": r["Invoice Date"], "Shipment Date": r["Shipment Date"],
                         "Tracking Number": r["Express or Ground Tracking ID"].strip(),
                         "Service Type": r["Service Type"], "Package Weight": r["Rated Weight Amount"],
                         "Zone": r["Zone Code"], "Net Charge": str(amt), "Source File": base(p)})
            i = inv[r["Invoice Number"]]; i["rows"] += 1; i["amount"] += amt; i["invoice_date"] = iso(r["Invoice Date"])
    path = os.path.join(out, f"fedex_append_{stamp}.csv"); write(path, rows)
    trk = [r["Tracking Number"] for r in rows]; ships = sorted(iso(r["Shipment Date"]) for r in rows if r["Shipment Date"])
    flags = []
    if skipped: flags.append(f"Left out {sum(skipped.values())} rows on {len(skipped)} FedEx invoices already loaded: {', '.join(sorted(skipped))}")
    if dup_rows: flags.append(f"Skipped {dup_rows} FedEx rows repeated across files (duplicate downloads)")
    if len(trk) != len(set(trk)): flags.append(f"{len(trk)-len(set(trk))} FedEx tracking numbers billed on more than one invoice (check for re-bills)")
    nonpos = sum(1 for r in rows if D(r["Net Charge"]) <= 0)
    if nonpos: flags.append(f"{nonpos} FedEx rows with zero/negative charge (credits or adjustments)")
    return {"file": os.path.basename(path), "sources": [base(f) for f in files], "packages": len(rows),
            "total": str(sum(D(r["Net Charge"]) for r in rows)), "ship_from": ships[0] if ships else None,
            "ship_to": ships[-1] if ships else None,
            "invoices": {k: {**v, "amount": str(v["amount"])} for k, v in sorted(inv.items())}, "flags": flags}

def process_stamps(files, out, stamp, after=None):
    rows, status, carriers, accts = [], Counter(), defaultdict(lambda: [0, Decimal("0")]), set()
    left_out = 0
    printed_amt, refunded = Decimal("0"), 0
    for p in files:
        m = re.search(r"PrintHistory_(\d+)_", base(p)); accts.add(m.group(1) if m else "unknown")
        for r in csv.DictReader(open(p, newline="", encoding="utf-8-sig")):
            if after and iso(r["Ship Date"]) <= after:  # already covered by an earlier week
                left_out += 1; continue
            amt = D(r["Amount Paid"])
            rows.append({"Ship Date": r["Ship Date"], "Tracking Number": clean_excel(r["Tracking #"]),
                         "Carrier": r["Carrier"], "Service": r["Service"], "Weight": r["Weight"],
                         "Amount Paid": str(amt), "To Name": (r["Recipient"] or "").split(",")[0],
                         "To ZIP": clean_excel(r["Postal Code"]), "Source File": base(p)})
            status[r["Shipment Status"]] += 1; c = carriers[r["Carrier"]]; c[0] += 1; c[1] += amt
            if r["Shipment Status"] == "Printed": printed_amt += amt
            if r["Shipment Status"] == "Refunded": refunded += 1
    path = os.path.join(out, f"stamps_append_{stamp}.csv"); write(path, rows)
    ships = sorted(iso(r["Ship Date"]) for r in rows if r["Ship Date"])
    flags = []
    if status.get("Printed"): flags.append(f"{status['Printed']} Stamps labels printed but not yet scanned (${printed_amt:,.2f})")
    if left_out: flags.append(f"Left out {left_out} Stamps labels shipped on or before {after} (already covered)")
    if refunded: flags.append(f"{refunded} Stamps labels refunded")
    return {"file": os.path.basename(path), "sources": [base(f) for f in files], "labels": len(rows),
            "total": str(sum(D(r["Amount Paid"]) for r in rows)), "ship_from": ships[0] if ships else None,
            "ship_to": ships[-1] if ships else None, "accounts": sorted(accts),
            "by_carrier": {k: {"labels": v[0], "amount": str(v[1])} for k, v in carriers.items()},
            "status": dict(status), "flags": flags}

def process_ups(files, out, stamp):
    lines = []
    for p in files:
        for x in csv.reader(open(p, newline="", encoding="utf-8-sig")):
            if len(x) < 53: continue
            lines.append(dict(inv_date=x[4], invoice=x[5], inv_amt=x[10], ship=x[11], trk=x[20], ref1=x[21],
                              bill=x[17], wt=x[28], zone=x[33], cat=x[43], desc=x[45], net=D(x[52]), src=base(p)))
    pk = {}
    for l in lines:
        if not l["trk"]: continue
        g = pk.setdefault(l["trk"], {"Invoice Date": l["inv_date"], "Invoice Number": l["invoice"], "Ship Date": l["ship"],
                                     "Bill Option": l["bill"], "Service": "", "Billed Weight": l["wt"], "Zone": l["zone"],
                                     "Reference 1": l["ref1"], "Net Charge": Decimal("0"), "Source File": l["src"]})
        g["Net Charge"] += l["net"]
        if l["cat"] == "FRT" and not g["Service"]: g["Service"] = l["desc"]
    rows = [{"Tracking Number": k, **{kk: (str(vv) if kk == "Net Charge" else vv) for kk, vv in v.items()}} for k, v in pk.items()]
    path = os.path.join(out, f"ups_append_{stamp}.csv"); write(path, rows)
    inv = {}
    for l in lines:
        i = inv.setdefault(l["invoice"], {"invoice_date": l["inv_date"], "stated": str(D(l["inv_amt"])), "line_sum": Decimal("0")})
        i["line_sum"] += l["net"]
    flags = [f"UPS invoice {k}: line sum {v['line_sum']} != stated {v['stated']}" for k, v in inv.items()
             if abs(v["line_sum"] - D(v["stated"])) > Decimal("0.05")]
    paid = [r for r in rows if D(r["Net Charge"]) != 0]
    acct_fees = sum((l["net"] for l in lines if not l["trk"]), Decimal("0"))
    return {"file": os.path.basename(path), "sources": [base(f) for f in files], "packages": len(rows),
            "paid_packages": len(paid), "third_party_zero": sum(1 for r in rows if r["Bill Option"] == "DTP" and D(r["Net Charge"]) == 0),
            "account_level_fees": str(acct_fees), "total": str(sum((l["net"] for l in lines), Decimal("0"))),
            "invoices": {k: {"invoice_date": v["invoice_date"], "amount": v["stated"]} for k, v in sorted(inv.items())},
            "flags": flags}

def write(path, rows):
    if not rows: return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("inputs", nargs="+"); ap.add_argument("--out", required=True)
    ap.add_argument("--last-stamps-account", help="previous Stamps account number, to flag a change")
    ap.add_argument("--skip-fedex-invoices", default="", help="comma-separated FedEx invoice numbers to leave out (already loaded)")
    ap.add_argument("--stamps-after", help="YYYY-MM-DD: keep only Stamps labels shipped after this date")
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    files = []
    for i in a.inputs:
        files += [os.path.join(i, f) for f in sorted(os.listdir(i))] if os.path.isdir(i) else [i]
    kinds, unknown = defaultdict(list), []
    for f in files:
        k = detect(f) if f.lower().endswith(".csv") else None
        (kinds[k].append(f) if k else unknown.append(os.path.basename(f)))
    stamp = datetime.now().strftime("%Y-%m-%d")
    summary = {"run_date": stamp, "unrecognized_files": unknown}
    if kinds["fedex"]: summary["fedex"] = process_fedex(kinds["fedex"], a.out, stamp, {x.strip() for x in a.skip_fedex_invoices.split(",") if x.strip()})
    if kinds["stamps"]:
        summary["stamps"] = process_stamps(kinds["stamps"], a.out, stamp, a.stamps_after)
        if a.last_stamps_account and a.last_stamps_account not in summary["stamps"]["accounts"]:
            summary["stamps"]["flags"].append(f"Stamps account changed: {a.last_stamps_account} -> {', '.join(summary['stamps']['accounts'])}")
    if kinds["ups"]: summary["ups"] = process_ups(kinds["ups"], a.out, stamp)
    for c in ("fedex", "stamps", "ups"):
        if c not in summary: summary.setdefault("missing_carriers", []).append(c)
    json.dump(summary, open(os.path.join(a.out, "summary.json"), "w"), indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str))

if __name__ == "__main__": main()

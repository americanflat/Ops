#!/usr/bin/env python3
"""Validate a FedEx invoice CSV against Stedi 945 shipping data in BigQuery.

Every FedEx invoice line (one tracking ID) is matched to the Stedi 945 ship
confirmations in `finance.shipment_reconciliation`, which already resolves each
945 order to a marketplace channel through `warehouse.channel_keys`.

Match order, first hit wins:
  1. FedEx tracking ID   = 945 cartonTracking
  2. FedEx Customer Ref  = 945 ssccBarcode  (the label's SSCC)
  3. FedEx Ref#2 / Ref#3 = 945 orderNumber / warehouseReference
     The order shipped, but FedEx billed a label that is not one of the cartons
     on its 945 (an extra or relabelled carton). matched_on = `order_ref`;
     these are listed separately for review.

Reships: the warehouse cannot process the same order number twice, so a
reship goes out as the original order number plus `R`, `R<n>` or `*<n>`
(27402R2, 4791738745R). A line whose 945 order number or FedEx reference has
that shape, and whose base is a known marketplace order, is resolved to that
marketplace with resolution `reship` and `original_order` set to that order.

Replacements: an order number starting `RPL` (945: RPL2651600F30F25, FedEx
ref: RPL-26516-00F30F25) is a Shopify replacement. It resolves to SHIPSTATION
with resolution `replacement`; `original_order` is the order number between the
dashes when a reference carries it.

Ship-to: an order still without a marketplace whose 945 ship-to name (N1*ST)
names the marketplace itself — "Wayfair Store Wilmette", "CastleGate Warehouse
Columbus" on the CG... Wayfair store/warehouse orders — resolves to that
marketplace with resolution `ship_to`.

940s: a line with no 945 at all is looked up in the 940 warehouse shipping
orders passed with --edi-940 (Stedi JSON, as files or directories). The 940's
W05 depositor / PO number is matched to the FedEx references, and the
marketplace is read from its N9*DP department (else N9*IA). Resolution `940`:
the order was sent to the warehouse but its shipment was never confirmed.
940s are not in BigQuery, so they have to be supplied.

Any line whose order still has no marketplace is then given a *suggested*
channel by looking its FedEx references (Ref#2, Ref#3/PO) up in
`warehouse.channel_keys` after stripping the decorations warehouses add:
`AME*`/`AMS*` prefixes, `-A` carton suffixes and `WHS-<n>-<po>` store
shipments. A suggestion is a lead to confirm, not a resolution.

Read-only: it only runs SELECT queries, so the session's read-only BigQuery
credential is enough. Needs the `bq` CLI.

    python3 tools/fedex_invoice_validate.py FedEx_invoice_*.CSV
    python3 tools/fedex_invoice_validate.py --out-dir out/ FedEx_invoice_*.CSV
    python3 tools/fedex_invoice_validate.py --edi-940 940s/ FedEx_invoice_*.CSV
"""

import argparse
import collections
import csv
import json
import os
import re
import subprocess
import sys
from decimal import Decimal

PROJECT = "americanflat"
RECON = f"`{PROJECT}.finance.shipment_reconciliation`"
CHANNEL_KEYS = f"`{PROJECT}.warehouse.channel_keys`"

# Keeps each query's inlined invoice literal well under BigQuery's 1 MB limit.
CHUNK = 4000

TRACKING = "Express or Ground Tracking ID"
REF1 = "Original Customer Reference"
REF2 = "Original Ref#2"
REF3 = "Original Ref#3/PO Number"

# Original order number + R, R<n> or *<n>: a reship of that order.
RESHIP = r"^(.+?)(?:R\d*|\*\d+)$"

# RPL... order numbers are replacements sent for Shopify orders.
REPLACEMENT_CHANNEL = "SHIPSTATION"

# 945 ship-to names that identify the marketplace on their own.
SHIP_TO_CHANNELS = [
    (r"\b(wayfair|castle\s*gate)\b", "WAYF"),
]


def clean_refs(*refs):
    """FedEx references with the warehouse's `AME*`/`AMS*` prefix removed."""
    for ref in refs:
        ref = (ref or "").strip()
        if ref and ref.upper() not in ("NONE", "NO REFERENCE INFORMATION"):
            yield re.sub(r"^[A-Z]{3}\*", "", ref)


def reship_bases(*refs):
    """Original order numbers that reship-shaped references point back to."""
    return list(dict.fromkeys(m.group(1) for ref in clean_refs(*refs)
                              if (m := re.match(RESHIP, ref))))


def ref_candidates(*refs):
    """Order keys a warehouse may have hidden inside a FedEx reference."""
    out = []
    for ref in clean_refs(*refs):
        out.append(ref)
        for pat in (r"^(.+?)-[A-Z]$",           # 4794150206-A
                    r"^WHS-\d+-(\d+)$"):        # WHS-10545-45628836
            m = re.match(pat, ref)
            if m:
                out.append(m.group(1))
    return list(dict.fromkeys(out))

# 940 N9*DP / N9*IA marketplace names -> channel codes used by channel_keys.
MARKETPLACE_CHANNELS = [
    (r"target", "TARG"), (r"walmart", "WALC"), (r"wayfair", "WAYF"),
    (r"macy", "MACY"), (r"kohl", "KOHLS"), (r"amazon", "AMZC"),
    (r"shopify|shipstation", "SHIPSTATION"),
]


def read_940s(paths):
    """Order number -> marketplace name from Stedi 940 JSON files."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += [os.path.join(path, n) for n in sorted(os.listdir(path)) if n.lower().endswith(".json")]
        else:
            files.append(path)

    found = {}

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            head = node.get("heading")
            if isinstance(head, dict) and "shipping_order_identification_W05" in head:
                w05 = head["shipping_order_identification_W05"]
                n9 = {}
                for key, val in head.items():
                    if key.startswith("reference_identification_N9"):
                        for ref in val if isinstance(val, list) else [val]:
                            n9.setdefault(ref.get("reference_identification_qualifier_01"),
                                          ref.get("reference_identification_02"))
                name = n9.get("DP") or n9.get("IA")
                if name:
                    for key in ("depositor_order_number_02", "purchase_order_number_03"):
                        if w05.get(key):
                            found[str(w05[key]).strip()] = name
            else:
                for v in node.values():
                    walk(v)

    for path in files:
        with open(path) as f:
            walk(json.load(f))
    return found


def marketplace_channel(name):
    return next((c for pat, c in MARKETPLACE_CHANNELS if re.search(pat, name, re.I)), name)


def read_invoice(paths):
    rows = []
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not r.get(TRACKING):
                    continue
                rows.append({
                    "source_file": os.path.basename(path),
                    "invoice_number": r["Invoice Number"],
                    "tracking_id": r[TRACKING].strip(),
                    "ship_date": r["Shipment Date"],
                    "service_type": r["Service Type"],
                    "shipper_city": r["Shipper City"],
                    "recipient": r["Recipient Name"] or r["Recipient Company"],
                    "ref1": r[REF1].strip(),
                    "ref2": r[REF2].strip(),
                    "ref3_po": r[REF3].strip(),
                    "net_charge": Decimal(r["Net Charge Amount"] or "0"),
                })
    return rows


def sql_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def sql_array(items):
    return f"ARRAY<STRING>[{','.join(sql_str(c) for c in items)}]"


def build_query(rows):
    structs = ",".join(
        f"({i},{sql_str(r['tracking_id'])},{sql_str(r['ref1'])},"
        f"{sql_array(ref_candidates(r['ref2'], r['ref3_po']))},"
        f"{sql_array(reship_bases(r['ref2'], r['ref3_po']))})"
        for i, r in rows)
    return f"""
WITH inv AS (
  SELECT * FROM UNNEST(ARRAY<STRUCT<i INT64, trk STRING, ref1 STRING, cands ARRAY<STRING>, rbases ARRAY<STRING>>>[{structs}])),
rec AS (
  SELECT DISTINCT cartonTracking, ssccBarcode, orderNumber, channel, resolutionStatus,
         ARRAY_TO_STRING(candidateChannels, '+') AS candidateChannels, warehouse, carrierScac, shipDate,
         warehouseReference, shipToName
  FROM {RECON}),
by_trk AS (SELECT * FROM rec WHERE cartonTracking IS NOT NULL
           QUALIFY ROW_NUMBER() OVER (PARTITION BY cartonTracking ORDER BY orderNumber) = 1),
by_sscc AS (SELECT * FROM rec WHERE ssccBarcode IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (PARTITION BY ssccBarcode ORDER BY orderNumber) = 1),
by_order AS (
  SELECT inv.i, ARRAY_AGG(o ORDER BY o.orderNumber LIMIT 1)[OFFSET(0)] AS o
  FROM inv JOIN (SELECT * FROM rec QUALIFY ROW_NUMBER() OVER (PARTITION BY orderNumber ORDER BY shipDate DESC) = 1) o
    ON o.orderNumber IN UNNEST(inv.cands) OR o.warehouseReference IN UNNEST(inv.cands)
  GROUP BY 1),
sug AS (
  SELECT inv.i, STRING_AGG(DISTINCT ck.channel, '+' ORDER BY ck.channel) AS suggested,
         STRING_AGG(DISTINCT ck.k, '+' ORDER BY ck.k) AS suggested_key
  FROM inv, UNNEST(inv.cands) c JOIN {CHANNEL_KEYS} ck ON ck.k = c
  GROUP BY 1),
m AS (
SELECT inv.i, inv.rbases,
  CASE WHEN t.orderNumber IS NOT NULL THEN 'tracking'
       WHEN s.orderNumber IS NOT NULL THEN 'sscc'
       WHEN bo.o.orderNumber IS NOT NULL THEN 'order_ref' END AS matched_on,
  COALESCE(t.orderNumber, s.orderNumber, bo.o.orderNumber) AS order_number,
  COALESCE(t.channel, s.channel, bo.o.channel) AS channel,
  COALESCE(t.resolutionStatus, s.resolutionStatus, bo.o.resolutionStatus, 'not_in_945') AS resolution,
  COALESCE(t.candidateChannels, s.candidateChannels, bo.o.candidateChannels) AS candidate_channels,
  COALESCE(t.warehouse, s.warehouse, bo.o.warehouse) AS warehouse_945,
  COALESCE(t.carrierScac, s.carrierScac, bo.o.carrierScac) AS carrier_945,
  CAST(COALESCE(t.shipDate, s.shipDate, bo.o.shipDate) AS STRING) AS ship_date_945,
  COALESCE(t.shipToName, s.shipToName, bo.o.shipToName) AS ship_to_945,
  sug.suggested AS suggested_channel, sug.suggested_key
FROM inv
LEFT JOIN by_trk t ON t.cartonTracking = inv.trk
LEFT JOIN by_sscc s ON t.orderNumber IS NULL AND inv.ref1 != '' AND s.ssccBarcode = inv.ref1
LEFT JOIN by_order bo ON t.orderNumber IS NULL AND s.orderNumber IS NULL AND bo.i = inv.i
LEFT JOIN sug ON sug.i = inv.i),
-- Reships: strip R / R<n> / *<n> from the 945 order number (or take the base
-- already stripped from the FedEx refs) and look the original order up.
rs AS (
  SELECT m.i, STRING_AGG(DISTINCT ck.channel, '+' ORDER BY ck.channel) AS ch,
         STRING_AGG(DISTINCT ck.k, '+' ORDER BY ck.k) AS base
  FROM m, UNNEST(ARRAY_CONCAT(m.rbases,
         IF(m.order_number IS NULL, [], [REGEXP_EXTRACT(m.order_number, r'{RESHIP}')]))) b
  JOIN {CHANNEL_KEYS} ck ON ck.k = b
  WHERE m.resolution != 'resolved'
  GROUP BY 1)
SELECT m.* EXCEPT (rbases, channel, resolution),
  IF(rs.ch IS NOT NULL AND NOT CONTAINS_SUBSTR(rs.ch, '+'), rs.ch, m.channel) AS channel,
  IF(rs.ch IS NOT NULL AND NOT CONTAINS_SUBSTR(rs.ch, '+'), 'reship', m.resolution) AS resolution,
  rs.base AS original_order
FROM m LEFT JOIN rs USING (i)
"""


def run_bq(sql):
    try:
        p = subprocess.run(
            ["bq", "query", f"--project_id={PROJECT}", "--use_legacy_sql=false",
             "--format=json", "--max_rows=1000000", "--quiet"],
            input=sql, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("bq CLI not found. Install the Google Cloud SDK and run `gcloud auth login`.")
    if p.returncode:
        sys.exit(f"BigQuery query failed:\n{p.stderr or p.stdout}")
    return json.loads(p.stdout or "[]")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("invoices", nargs="+", help="FedEx invoice CSV export(s)")
    ap.add_argument("--out-dir", default=".", help="where to write the result CSVs")
    ap.add_argument("--edi-940", nargs="*", default=[], metavar="PATH",
                    help="Stedi 940 JSON files or directories, for orders with no 945")
    args = ap.parse_args()

    rows = read_invoice(args.invoices)
    orders_940 = read_940s(args.edi_940)
    if not rows:
        sys.exit("No invoice lines with a tracking ID found.")

    indexed = list(enumerate(rows))
    for start in range(0, len(indexed), CHUNK):
        for m in run_bq(build_query(indexed[start:start + CHUNK])):
            rows[int(m.pop("i"))].update({k: v or "" for k, v in m.items()})
    for r in rows:
        if r["resolution"] in ("resolved", "reship"):
            continue
        refs = [r["order_number"], *clean_refs(r["ref2"], r["ref3_po"])]
        if any(ref.upper().startswith("RPL") for ref in refs):
            orig = next((m.group(1) for ref in refs if (m := re.match(r"^RPL-(\d+)-", ref, re.I))), "")
            r.update(channel=REPLACEMENT_CHANNEL, resolution="replacement", original_order=orig)
            continue
        ch = next((c for pat, c in SHIP_TO_CHANNELS if re.search(pat, r["ship_to_945"], re.I)), None)
        if ch:
            r.update(channel=ch, resolution="ship_to")
            continue
        if r["resolution"] == "not_in_945":
            hit = next((ref for ref in refs if ref in orders_940), None)
            if hit:
                r.update(channel=marketplace_channel(orders_940[hit]), resolution="940", order_number=hit)

    os.makedirs(args.out_dir, exist_ok=True)
    fields = list(rows[0].keys())
    all_path = os.path.join(args.out_dir, "fedex_validation_lines.csv")
    miss_path = os.path.join(args.out_dir, "fedex_orders_without_marketplace.csv")
    with open(all_path, "w", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)
    missing = [r for r in rows if r["resolution"] not in ("resolved", "reship", "replacement", "ship_to", "940")]
    with open(miss_path, "w", newline="") as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(sorted(missing, key=lambda r: (r["resolution"], r["order_number"], r["tracking_id"])))

    # Summary
    total = sum(r["net_charge"] for r in rows)
    invoices = sorted({r["invoice_number"] for r in rows})
    print(f"FedEx invoice lines: {len(rows)}  across {len(invoices)} invoice(s)  net ${total:,.2f}")
    by_res = collections.Counter(r["resolution"] for r in rows)
    print("945 match: " + ", ".join(f"{k} {v}" for k, v in sorted(by_res.items())))
    print()
    print(f"{'Marketplace':<14}{'Lines':>7}{'Orders':>8}{'Net charge':>14}{'Avg/line':>10}")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["channel"] or "(none)"].append(r)
    for ch, rs in sorted(groups.items(), key=lambda kv: -sum(r["net_charge"] for r in kv[1])):
        net = sum(r["net_charge"] for r in rs)
        orders = len({r["order_number"] for r in rs if r["order_number"]})
        print(f"{ch:<14}{len(rs):>7}{orders:>8}{net:>14,.2f}{net / len(rs):>10,.2f}")

    reships = [r for r in rows if r["resolution"] in ("reship", "replacement")]
    if reships:
        print()
        print(f"Reships and replacements, resolved through the original order: {len(reships)} lines")
        for r in sorted(reships, key=lambda r: (r["resolution"], r["order_number"] or r["ref2"])):
            print(f"  {r['order_number'] or r['ref2']:<18} -> {r['channel']:<12} {r['resolution']} of"
                  f" {r['original_order'] or '?':<12} trk={r['tracking_id']:<13} ${r['net_charge']:>7}")

    off_945 = [r for r in rows if r["matched_on"] == "order_ref"]
    if off_945:
        print()
        print(f"Billed labels not on the order's 945 (extra or relabelled carton): {len(off_945)} lines,"
              f" ${sum(r['net_charge'] for r in off_945):,.2f}")
        for r in sorted(off_945, key=lambda r: r["order_number"]):
            print(f"  order={r['order_number']:<18} {r['channel'] or '(none)':<12} trk={r['tracking_id']:<13}"
                  f" 945 ship {r['ship_date_945']}  ${r['net_charge']:>7}")

    by_940 = [r for r in rows if r["resolution"] == "940"]
    if by_940:
        print()
        print(f"No 945, marketplace from the 940 (shipped but never confirmed): {len(by_940)} lines")
        for r in sorted(by_940, key=lambda r: r["order_number"]):
            print(f"  order={r['order_number']:<18} -> {r['channel']:<12} trk={r['tracking_id']:<13}"
                  f" ${r['net_charge']:>7}")
    elif args.edi_940 == [] and any(r["resolution"] == "not_in_945" for r in rows):
        print()
        print("Lines with no 945 can be resolved from their 940s: rerun with --edi-940 <files or dir>.")

    by_ship_to = [r for r in rows if r["resolution"] == "ship_to"]
    if by_ship_to:
        print()
        print(f"Resolved from the 945 ship-to name: {len(by_ship_to)} lines")
        for r in sorted(by_ship_to, key=lambda r: r["order_number"]):
            print(f"  {r['order_number']:<18} -> {r['channel']:<12} ship-to {r['ship_to_945']:<28}"
                  f" trk={r['tracking_id']:<13} ${r['net_charge']:>7}")

    print()
    print(f"Orders with no marketplace identifier: {len({(r['order_number'] or r['tracking_id']) for r in missing})}"
          f" ({len(missing)} lines, ${sum(r['net_charge'] for r in missing):,.2f})")
    for r in sorted(missing, key=lambda r: (r["resolution"], r["order_number"])):
        sug = f"  -> suggest {r['suggested_channel']} ({r['suggested_key']})" if r["suggested_channel"] else ""
        print(f"  {r['resolution']:<11} order={r['order_number'] or '-':<18} trk={r['tracking_id']:<13}"
              f" ref2={r['ref2'] or '-':<24} {r['service_type']:<25} ${r['net_charge']:>7}{sug}")
    print()
    print(f"Wrote {all_path}")
    print(f"Wrote {miss_path}")


if __name__ == "__main__":
    main()

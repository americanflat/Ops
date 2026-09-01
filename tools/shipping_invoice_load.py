#!/usr/bin/env python3
"""Load weekly Stamps.com / FedEx / UPS invoice CSVs into BigQuery.

    python3 tools/shipping_invoice_load.py review <file-or-dir> [...]
    python3 tools/shipping_invoice_load.py load   <file-or-dir> [...]

`review` parses everything and prints what would be written — row counts,
totals per carrier, and anything that failed to parse — without touching
BigQuery. Always look at the money before loading; these are invoices.

`load` does the same parse and then appends to
`americanflat.finance.shipping_invoices` (see sql/finance_shipping_invoices.sql
for the DDL and docs/shipping-invoices.md for the whole pipeline).

Why the shape it has
--------------------
Carriers rename columns, add surcharges, and reorder files without warning.
So the parser maps *known* headers onto a canonical schema by alias and drops
every column it does not recognise into the `raw` JSON string. A file with an
unfamiliar column still loads, losslessly, and stays queryable — no code change
needed to survive a carrier's format change, only to promote a new column to
first class.

Grain is one row per charge line. FedEx bills a package as several tracked
charges (transportation, fuel, residential); Stamps.com bills one flat postage
amount. Both land in the same table; `finance.shipping_invoice_packages` rolls
them back up to one row per tracking number.

Credentials come from the agent proxy, which injects them for
bigquery.googleapis.com. There is no key to configure.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = "americanflat"
DATASET = os.environ.get("SHIPPING_BQ_DATASET", "finance")
TABLE = os.environ.get("SHIPPING_BQ_TABLE", "shipping_invoices")
FQ_TABLE = f"{PROJECT}.{DATASET}.{TABLE}"
BQ = "https://bigquery.googleapis.com"

# Columns in load order. Must match sql/finance_shipping_invoices.sql.
SCHEMA = [
    ("row_id", "STRING"), ("carrier", "STRING"), ("invoice_number", "STRING"),
    ("invoice_date", "DATE"), ("account_number", "STRING"), ("ship_date", "DATE"),
    ("tracking_number", "STRING"), ("service", "STRING"),
    ("charge_description", "STRING"), ("charge_category", "STRING"),
    ("amount", "NUMERIC"), ("adjusted_amount", "NUMERIC"), ("currency", "STRING"),
    ("weight_lbs", "NUMERIC"), ("billed_weight_lbs", "NUMERIC"), ("zone", "STRING"),
    ("reference_1", "STRING"), ("reference_2", "STRING"), ("reference_3", "STRING"),
    ("order_id", "STRING"), ("cost_code", "STRING"), ("recipient_name", "STRING"),
    ("recipient_city", "STRING"), ("recipient_state", "STRING"),
    ("recipient_zip", "STRING"), ("recipient_country", "STRING"),
    ("shipper_city", "STRING"), ("raw", "STRING"), ("source_file", "STRING"),
    ("ingested_at", "TIMESTAMP"), ("ingested_by", "STRING"),
]

# --------------------------------------------------------------------------
# header handling
# --------------------------------------------------------------------------

def norm(header: str) -> str:
    """'Original Ref#3/PO Number' -> 'original_ref_3_po_number'.

    Carriers vary punctuation and casing between exports far more often than
    they vary the words, so all matching happens on this normalised form.
    """
    h = (header or "").strip().lower()
    h = re.sub(r"#\s*(\d)", r"_\1", h)          # 'ref#3' -> 'ref_3'
    h = re.sub(r"[^a-z0-9]+", "_", h)
    return h.strip("_")


# canonical field -> aliases (normalised). First alias present in the file wins.
COMMON_ALIASES = {
    "invoice_number": ["invoice_number", "invoice", "invoice_no", "invoice_nbr"],
    "invoice_date": ["invoice_date", "invoice_dt"],
    "account_number": ["account_number", "bill_to_account_number",
                       "payor_account_number", "shipper_number", "account"],
    "tracking_number": ["express_or_ground_tracking_id", "tracking_number",
                        "tracking", "tracking_id", "ground_tracking_id",
                        "package_tracking_number", "lead_shipment_number"],
    "ship_date": ["ship_date", "shipment_date", "pickup_date", "transaction_date",
                  "date_shipped", "shipped_date", "date_printed", "print_date"],
    "service": ["service", "service_type", "service_level", "service_level_detail",
                "mail_class", "shipping_service"],
    "zone": ["zone", "zone_code", "rated_zone", "billed_zone"],
    "weight_lbs": ["actual_weight_amount", "entered_weight", "weight",
                   "package_weight", "actual_weight", "weight_lbs", "weight_oz",
                   "weight_in_oz", "weight_lb"],
    "billed_weight_lbs": ["rated_weight_amount", "billed_weight", "billed_wt",
                          "rated_weight", "dim_weight"],
    "currency": ["currency", "net_charge_currency", "invoice_currency_code"],
    "recipient_name": ["recipient_name", "recipient_company_name", "to_name",
                       "receiver_name", "ship_to_name", "to_company"],
    "recipient_city": ["recipient_city", "to_city", "receiver_city", "ship_to_city"],
    "recipient_state": ["recipient_state", "to_state", "receiver_state",
                        "recipient_state_province", "ship_to_state"],
    "recipient_zip": ["recipient_zip_code", "recipient_postal_code", "to_zip",
                      "receiver_postal", "ship_to_zip", "to_postal_code"],
    "recipient_country": ["recipient_country", "to_country", "receiver_country",
                          "recipient_country_code", "ship_to_country"],
    "shipper_city": ["shipper_city", "from_city", "sender_city"],
    "reference_1": ["original_customer_reference", "shipment_reference_number_1",
                    "reference_1", "reference", "customer_reference", "ref_1"],
    "reference_2": ["original_ref_2", "shipment_reference_number_2",
                    "reference_2", "printed_message", "ref_2"],
    "reference_3": ["original_ref_3_po_number", "shipment_reference_number_3",
                    "reference_3", "po_number", "ref_3"],
    "order_id": ["order_id", "order_number", "order"],
    "cost_code": ["cost_code", "cost_center"],
    "amount": ["net_charge_amount", "net_amount", "amount_paid", "charge_amount",
               "amount", "total_charge", "billed_amount", "incentive_amount_net"],
    "adjusted_amount": ["adjusted_amount", "adjustment_amount"],
    "charge_description": ["charge_description", "tracked_charge_description",
                           "charge_classification_detail", "charge_type"],
}

# What a file has to contain to be recognised as a given carrier. Checked in
# order; first carrier whose signature is satisfied wins.
SIGNATURES = [
    ("FedEx", lambda h: "express_or_ground_tracking_id" in h
                        or ("net_charge_amount" in h and "invoice_number" in h)),
    ("Stamps.com", lambda h: "amount_paid" in h
                             and any(k in h for k in ("tracking", "tracking_number"))),
    ("UPS", lambda h: "tracking_number" in h
                      and ("charge_description" in h or "net_amount" in h)),
]

TRACKED_DESC = re.compile(r"^tracked_charge_description_?(\d+)$")
TRACKED_AMT = re.compile(r"^tracked_charge_amount_?(\d+)$")


def detect_carrier(headers) -> str | None:
    h = set(headers)
    for name, test in SIGNATURES:
        if test(h):
            return name
    return None


# --------------------------------------------------------------------------
# value parsing
# --------------------------------------------------------------------------

MONEY = re.compile(r"^\(?\s*-?\s*[$]?\s*[\d,]*\.?\d*\s*\)?$")


def parse_money(value):
    """'$1,234.50' -> 1234.50; '(12.00)' and '-12.00' -> -12.00; '' -> None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s in {"-", "--", "N/A", "NA"}:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace("USD", "").strip()
    if not s or not re.match(r"^-?\d*\.?\d+$", s):
        return None
    out = float(s)
    return -out if negative and out > 0 else out


DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y",
                "%d-%b-%y", "%b %d, %Y", "%m-%d-%Y", "%Y/%m/%d")


def parse_date(value):
    """Return YYYY-MM-DD, or None. Times are tolerated and discarded."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = re.split(r"[ T]", s)[0] if re.search(r"[ T]\d{1,2}:", s) else s
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


WEIGHT_LB_OZ = re.compile(r"(?:(\d+(?:\.\d+)?)\s*lbs?)?\s*(?:(\d+(?:\.\d+)?)\s*oz)?",
                          re.I)


def parse_weight(value, header=""):
    """Pounds. Handles '2', '2.5 lbs', '1 lbs 4 oz', and oz-named columns."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.search(r"lbs?|oz", s, re.I):
        m = WEIGHT_LB_OZ.match(s)
        lbs = float(m.group(1)) if m and m.group(1) else 0.0
        oz = float(m.group(2)) if m and m.group(2) else 0.0
        total = lbs + oz / 16.0
        return round(total, 4) if total else None
    plain = parse_money(s)
    if plain is None:
        return None
    if "oz" in header and "lb" not in header:
        return round(plain / 16.0, 4)
    return plain


def clean_tracking(value):
    """Strip the spaces carriers print inside tracking numbers."""
    if value is None:
        return None
    s = re.sub(r"\s+", "", str(value)).strip()
    return s or None


def clean(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


CATEGORY_RULES = [
    ("adjustment", ("adjust", "audit", "correction", "refund", "credit", "rebate")),
    ("fuel", ("fuel",)),
    ("base", ("net charge", "postage", "transportation", "base", "freight",
              "ground", "express", "priority", "first class", "shipping charge")),
    ("surcharge", ("surcharge", "residential", "delivery area", "additional handling",
                   "oversize", "over size", "large package", "peak", "demand",
                   "signature", "address correction", "declared value", "insurance",
                   "saturday", "return", "dimensional", "handling", "fee", "tax",
                   "duty", "pickup", "accessorial", "unauthorized")),
]


def categorize(description: str) -> str:
    d = (description or "").lower()
    for category, needles in CATEGORY_RULES:
        if any(n in d for n in needles):
            return category
    return "other"


# --------------------------------------------------------------------------
# parsing a file into rows
# --------------------------------------------------------------------------

class ParseError(Exception):
    pass


def read_csv(path: Path):
    """Rows as dicts keyed by normalised header, plus the header list."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect)
        try:
            header = next(reader)
        except StopIteration:
            raise ParseError("file is empty")
        keys = [norm(h) for h in header]
        if not any(keys):
            raise ParseError("no usable header row")
        rows = []
        for values in reader:
            if not any(str(v).strip() for v in values):
                continue
            row = {}
            for i, key in enumerate(keys):
                if not key:
                    continue
                value = values[i] if i < len(values) else ""
                # Duplicate headers: keep the first non-empty value.
                if key in row and clean(row[key]) is not None:
                    continue
                row[key] = value
            rows.append(row)
    return keys, rows


def pick(row, field):
    for alias in COMMON_ALIASES.get(field, []):
        if alias in row:
            value = clean(row[alias])
            if value is not None:
                return value, alias
    return None, None


def charge_lines(row, keys, carrier):
    """[(description, amount, used_keys)] for one source row.

    FedEx invoice exports carry the surcharges as numbered
    'Tracked Charge Description N' / 'Tracked Charge Amount N' pairs; when
    those are present they are the truth and the net charge is only a
    reconciliation total. Everything else bills one amount per row.
    """
    pairs = {}
    for key in keys:
        m = TRACKED_DESC.match(key)
        if m:
            pairs.setdefault(m.group(1), {})["desc"] = key
        m = TRACKED_AMT.match(key)
        if m:
            pairs.setdefault(m.group(1), {})["amt"] = key

    lines, used = [], set()
    for idx in sorted(pairs, key=lambda s: int(s)):
        desc_key, amt_key = pairs[idx].get("desc"), pairs[idx].get("amt")
        if not amt_key:
            continue
        amount = parse_money(row.get(amt_key))
        description = clean(row.get(desc_key)) if desc_key else None
        used.update(k for k in (desc_key, amt_key) if k)
        if amount is None or (amount == 0 and not description):
            continue
        lines.append((description or f"Charge {idx}", amount))
    if lines:
        return lines, used

    description, desc_key = pick(row, "charge_description")
    amount, amt_key = pick(row, "amount")
    used = {k for k in (desc_key, amt_key) if k}
    value = parse_money(amount)
    if value is None:
        return [], used
    if not description:
        description = "Postage" if carrier == "Stamps.com" else "Net Charge"
    return [(description, value)], used


MAPPED_FIELDS = ["invoice_number", "invoice_date", "account_number", "ship_date",
                 "tracking_number", "service", "zone", "weight_lbs",
                 "billed_weight_lbs", "currency", "recipient_name", "recipient_city",
                 "recipient_state", "recipient_zip", "recipient_country",
                 "shipper_city", "reference_1", "reference_2", "reference_3",
                 "order_id", "cost_code", "adjusted_amount"]


def parse_file(path: Path, carrier_override=None, ingested_by="unknown"):
    keys, raw_rows = read_csv(path)
    carrier = carrier_override or detect_carrier(keys)
    if carrier is None:
        raise ParseError(
            "could not tell which carrier this file is from. Headers seen: "
            + ", ".join(keys[:12])
            + ". Pass --carrier FedEx|UPS|Stamps.com to force it.")

    now = datetime.now(timezone.utc).isoformat()
    source_file = path.name
    seen = defaultdict(int)
    out, skipped = [], 0

    for raw_row in raw_rows:
        used = set()
        values, matched_key = {}, {}
        for field in MAPPED_FIELDS:
            value, key = pick(raw_row, field)
            if key:
                used.add(key)
            values[field] = value
            matched_key[field] = key or ""

        lines, line_keys = charge_lines(raw_row, keys, carrier)
        used |= line_keys
        if not lines:
            skipped += 1
            continue

        leftovers = {k: v.strip() for k, v in raw_row.items()
                     if k not in used and str(v).strip()}

        for description, amount in lines:
            tracking = clean_tracking(values["tracking_number"])
            ship = parse_date(values["ship_date"])
            key = (carrier, values["invoice_number"], tracking, description,
                   f"{amount:.4f}", ship)
            seq = seen[key]
            seen[key] += 1
            digest = hashlib.sha256(
                "|".join([str(p) for p in key] + [source_file, str(seq)]).encode()
            ).hexdigest()

            out.append({
                "row_id": digest,
                "carrier": carrier,
                "invoice_number": values["invoice_number"],
                "invoice_date": parse_date(values["invoice_date"]),
                "account_number": values["account_number"],
                "ship_date": ship,
                "tracking_number": tracking,
                "service": values["service"],
                "charge_description": description,
                "charge_category": categorize(description),
                "amount": round(amount, 4),
                "adjusted_amount": parse_money(values["adjusted_amount"]),
                "currency": (values["currency"] or "USD").upper()[:3],
                "weight_lbs": parse_weight(values["weight_lbs"],
                                           matched_key["weight_lbs"]),
                "billed_weight_lbs": parse_weight(values["billed_weight_lbs"],
                                                  matched_key["billed_weight_lbs"]),
                "zone": values["zone"],
                "reference_1": values["reference_1"],
                "reference_2": values["reference_2"],
                "reference_3": values["reference_3"],
                "order_id": values["order_id"],
                "cost_code": values["cost_code"],
                "recipient_name": values["recipient_name"],
                "recipient_city": values["recipient_city"],
                "recipient_state": values["recipient_state"],
                "recipient_zip": values["recipient_zip"],
                "recipient_country": values["recipient_country"],
                "shipper_city": values["shipper_city"],
                "raw": json.dumps(leftovers, sort_keys=True) if leftovers else None,
                "source_file": source_file,
                "ingested_at": now,
                "ingested_by": ingested_by,
            })
    return carrier, out, skipped


def collect_files(paths):
    found = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found.extend(sorted(q for q in p.iterdir()
                                if q.suffix.lower() in {".csv", ".tsv", ".txt"}))
        elif p.exists():
            found.append(p)
        else:
            print(f"WARNING: no such file: {p}", file=sys.stderr)
    return found


# --------------------------------------------------------------------------
# BigQuery
# --------------------------------------------------------------------------

def bq_query(sql: str):
    req = urllib.request.Request(
        f"{BQ}/bigquery/v2/projects/{PROJECT}/queries",
        data=json.dumps({"query": sql, "useLegacySql": False,
                         "timeoutMs": 90000}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(out["error"].get("message", "unknown BigQuery error"))
    return out


def sql_str(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def already_loaded(source_files):
    """{source_file: row count} for files already in the table."""
    names = ", ".join(sql_str(f) for f in sorted(set(source_files)))
    out = bq_query(f"""
        SELECT source_file, COUNT(*) AS n
        FROM `{FQ_TABLE}`
        WHERE source_file IN ({names})
        GROUP BY source_file
    """)
    return {r["f"][0]["v"]: int(r["f"][1]["v"]) for r in out.get("rows", [])}


def delete_files(source_files):
    names = ", ".join(sql_str(f) for f in sorted(set(source_files)))
    bq_query(f"DELETE FROM `{FQ_TABLE}` WHERE source_file IN ({names})")


def load_rows(rows):
    """Append rows with a load job (multipart NDJSON upload). Returns job id."""
    ndjson = "\n".join(json.dumps(r) for r in rows).encode()
    job = {
        "configuration": {
            "load": {
                "destinationTable": {"projectId": PROJECT, "datasetId": DATASET,
                                     "tableId": TABLE},
                "sourceFormat": "NEWLINE_DELIMITED_JSON",
                "writeDisposition": "WRITE_APPEND",
                "createDisposition": "CREATE_NEVER",
                "schema": {"fields": [{"name": n, "type": t} for n, t in SCHEMA]},
                "ignoreUnknownValues": False,
            }
        }
    }
    boundary = "shipping-invoice-load-boundary"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
               .encode())
    body.write(json.dumps(job).encode())
    body.write(f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
               .encode())
    body.write(ndjson)
    body.write(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{BQ}/upload/bigquery/v2/projects/{PROJECT}/jobs?uploadType=multipart",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        started = json.loads(resp.read())
    job_id = started["jobReference"]["jobId"]
    location = started["jobReference"].get("location", "US")

    for _ in range(120):
        get = urllib.request.Request(
            f"{BQ}/bigquery/v2/projects/{PROJECT}/jobs/{job_id}?location={location}")
        with urllib.request.urlopen(get, timeout=60) as resp:
            status = json.loads(resp.read())["status"]
        if status.get("state") == "DONE":
            if "errorResult" in status:
                raise RuntimeError(status["errorResult"].get("message", "load failed"))
            return job_id
        import time
        time.sleep(2)
    raise RuntimeError(f"load job {job_id} did not finish in time")


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def summarize(parsed):
    print(f"\n{'file':<44} {'carrier':<11} {'rows':>7} {'amount':>14}  dates")
    print("-" * 100)
    grand_rows = grand_amount = 0
    for path, carrier, rows, skipped in parsed:
        amount = sum(r["amount"] for r in rows)
        dates = sorted({r["ship_date"] for r in rows if r["ship_date"]})
        span = f"{dates[0]} .. {dates[-1]}" if dates else "no ship dates parsed"
        print(f"{path.name[:44]:<44} {carrier:<11} {len(rows):>7} "
              f"{amount:>14,.2f}  {span}")
        if skipped:
            print(f"{'':<44} {'':<11} {'':>7} {'':>14}  "
                  f"({skipped} source rows had no parseable amount, skipped)")
        grand_rows += len(rows)
        grand_amount += amount
    print("-" * 100)
    print(f"{'TOTAL':<44} {'':<11} {grand_rows:>7} {grand_amount:>14,.2f}")

    by_category = defaultdict(float)
    no_tracking = no_date = 0
    for _, _, rows, _ in parsed:
        for r in rows:
            by_category[r["charge_category"]] += r["amount"]
            no_tracking += r["tracking_number"] is None
            no_date += r["ship_date"] is None
    print("\ncharge mix:", ", ".join(
        f"{k} {v:,.2f}" for k, v in sorted(by_category.items(), key=lambda kv: -kv[1])))
    if no_tracking:
        print(f"NOTE: {no_tracking} rows have no tracking number "
              f"(fine for account-level fees, a problem for shipment charges)")
    if no_date:
        print(f"NOTE: {no_date} rows have no ship date — they land in the "
              f"table's NULL partition")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["review", "load"])
    ap.add_argument("paths", nargs="+", help="CSV files, or directories of them")
    ap.add_argument("--carrier", choices=["FedEx", "UPS", "Stamps.com"],
                    help="force the carrier instead of detecting it from headers")
    ap.add_argument("--replace", action="store_true",
                    help="delete rows already loaded from these filenames first")
    ap.add_argument("--allow-duplicate", action="store_true",
                    help="load even though these filenames are already present")
    ap.add_argument("--ingested-by", default=os.environ.get("USER", "unknown"))
    ap.add_argument("--ndjson", help="also write the parsed rows here, for inspection")
    args = ap.parse_args()

    files = collect_files(args.paths)
    if not files:
        sys.exit("No input files found.")

    parsed, failures = [], []
    for path in files:
        try:
            carrier, rows, skipped = parse_file(path, args.carrier, args.ingested_by)
            parsed.append((path, carrier, rows, skipped))
        except (ParseError, OSError) as exc:
            failures.append((path, str(exc)))

    for path, message in failures:
        print(f"SKIPPED {path.name}: {message}", file=sys.stderr)
    if not parsed:
        sys.exit("Nothing parsed.")

    summarize(parsed)
    rows = [r for _, _, file_rows, _ in parsed for r in file_rows]

    if args.ndjson:
        Path(args.ndjson).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        print(f"\nwrote {len(rows)} rows to {args.ndjson}")

    if args.action == "review":
        print(f"\nReview only — nothing written. Re-run with `load` to append to "
              f"`{FQ_TABLE}`.")
        return

    source_files = [p.name for p, _, _, _ in parsed]
    existing = already_loaded(source_files)
    if existing:
        listed = ", ".join(f"{k} ({v} rows)" for k, v in existing.items())
        if args.replace:
            print(f"\nReplacing already-loaded files: {listed}")
            delete_files(list(existing))
        elif not args.allow_duplicate:
            sys.exit(f"\nAlready loaded: {listed}\n"
                     f"Re-run with --replace to reload them, or --allow-duplicate "
                     f"to append anyway.")

    job_id = load_rows(rows)
    print(f"\nLoaded {len(rows)} rows into `{FQ_TABLE}` (job {job_id}).")
    if failures:
        print(f"{len(failures)} file(s) were skipped — see above.")


if __name__ == "__main__":
    main()

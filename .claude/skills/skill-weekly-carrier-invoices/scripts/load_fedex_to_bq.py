#!/usr/bin/env python3
"""Append a FedEx invoice CSV export to americanflat.finance.fedex_shipping_costs.

Usage:
    python3 scripts/load_fedex_to_bq.py <fedex.csv> [<fedex2.csv> ...]            # dry run (default)
    python3 scripts/load_fedex_to_bq.py <fedex.csv> ... --apply                     # actually load

How it works:
  1. Reads the table's LIVE schema (`bq show --schema`) so column names are never
     hardcoded. FedEx CSV headers are matched to BQ columns by normalized name
     ("Express or Ground Tracking ID" -> express_or_ground_tracking_id).
  2. Refuses to run if the CSV is missing any REQUIRED BQ column.
  3. Skips (invoice_number, tracking_id) pairs already in the table, so re-running a
     week, or overlap with another pipeline, never double-counts.
  4. Prints invoice totals for sign-off. Nothing is written without --apply.

Auth: impersonates the service account below via gcloud (no key files), same pattern
as skill-invoice-to-bigquery. Never creates tables or IAM bindings.
"""
import argparse, csv, json, os, re, shutil, subprocess, sys, tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from pathlib import Path

_CFG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_CFG = json.loads(_CFG_PATH.read_text()) if _CFG_PATH.exists() else {}
_CFG = _CFG.get("bigquery", _CFG)  # this skill nests BQ settings under "bigquery"
# Config resolution (later wins): config.json next to the skill, then env vars.
PROJECT = os.environ.get("FEDEX_BQ_PROJECT", _CFG.get("project_id", "americanflat"))
DATASET = os.environ.get("FEDEX_BQ_DATASET", _CFG.get("dataset", "finance"))
TABLE = os.environ.get("FEDEX_BQ_TABLE", _CFG.get("table", "fedex_shipping_costs"))
SA = os.environ.get("FEDEX_BQ_SA", _CFG.get("impersonate_service_account",
                    "invoice-writer@americanflat.iam.gserviceaccount.com"))
LOCATION = os.environ.get("FEDEX_BQ_LOCATION", _CFG.get("location", "US"))
FQ = f"{PROJECT}.{DATASET}.{TABLE}"

# Columns the loader stamps itself if they exist in the table.
PROVENANCE = {"source_file", "loaded_at", "loaded_by"}
# Candidate key columns for de-duplication (first match in the schema wins).
INVOICE_KEYS = ["invoice_number", "invoice_no", "invoice_id"]
TRACKING_KEYS = ["express_or_ground_tracking_id", "tracking_id", "tracking_number"]


def norm(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", name.lower())).strip("_")


def bq(args, capture=True):
    env = dict(os.environ, CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=SA)
    cmd = [shutil.which("bq") or "bq", f"--project_id={PROJECT}", f"--location={LOCATION}"] + args
    res = subprocess.run(cmd, capture_output=capture, text=True, env=env)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        if "PERMISSION" in err.upper() or "403" in err:
            sys.exit(f"PERMISSION DENIED - nothing was written.\nAsk the BQ admin to grant "
                     f"BigQuery Data Editor on {FQ} to {SA}, and confirm you can impersonate it.\n{err}")
        sys.exit(f"bq failed: {err}")
    return res.stdout


def get_schema(schema_file=None):
    if schema_file:
        return json.load(open(schema_file))
    return json.loads(bq(["show", "--schema", "--format=json", f"{PROJECT}:{DATASET}.{TABLE}"]))


def cast(value, bq_type):
    v = (value or "").strip()
    if v == "":
        return None
    t = bq_type.upper()
    try:
        if t in ("NUMERIC", "BIGNUMERIC", "FLOAT", "FLOAT64"):
            return str(Decimal(v.replace(",", "").replace("$", "")))
        if t in ("INTEGER", "INT64"):
            return int(Decimal(v.replace(",", "")))
        if t == "DATE":
            for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
                try:
                    return datetime.strptime(v, fmt).date().isoformat()
                except ValueError:
                    pass
            return None
        if t == "BOOLEAN":
            return v.lower() in ("y", "yes", "true", "1")
    except (InvalidOperation, ValueError):
        return None
    return v


def first_present(cols, candidates):
    return next((c for c in candidates if c in cols), None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--apply", action="store_true", help="write to BigQuery (default is dry run)")
    ap.add_argument("--schema-file", help="offline testing: use a saved schema JSON instead of bq")
    a = ap.parse_args()

    schema = get_schema(a.schema_file)
    cols = {f["name"]: f for f in schema}
    required = {n for n, f in cols.items() if f.get("mode") == "REQUIRED" and n not in PROVENANCE}

    rows, unmapped_csv, per_invoice = [], set(), defaultdict(lambda: [0, Decimal("0")])
    mapped_cols = set()
    for path in a.files:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            header_map = {}
            for h in reader.fieldnames:
                if norm(h) in cols:
                    header_map[h] = norm(h)
                else:
                    unmapped_csv.add(h)
            mapped_cols |= set(header_map.values())
            missing = required - set(header_map.values())
            if missing:
                sys.exit(f"{os.path.basename(path)} is missing REQUIRED table columns: "
                         f"{sorted(missing)}. Nothing was written.")
            for r in reader:
                row = {bqc: cast(r.get(h), cols[bqc]["type"]) for h, bqc in header_map.items()}
                if "source_file" in cols:
                    row["source_file"] = os.path.basename(path)
                rows.append(row)
                inv = r.get("Invoice Number", "?")
                per_invoice[inv][0] += 1
                per_invoice[inv][1] += Decimal((r.get("Net Charge Amount") or "0").replace(",", "") or "0")

    inv_col = first_present(cols, INVOICE_KEYS)
    trk_col = first_present(cols, TRACKING_KEYS)

    # De-dupe against what is already loaded.
    existing = set()
    if inv_col and trk_col and not a.schema_file:
        invs = sorted({str(r.get(inv_col)) for r in rows if r.get(inv_col)})
        if invs:
            in_list = ",".join("'" + i.replace("'", "") + "'" for i in invs)
            out = bq(["query", "--nouse_legacy_sql", "--format=json", "--max_rows=1000000",
                      f"SELECT CAST({inv_col} AS STRING) i, CAST({trk_col} AS STRING) t "
                      f"FROM `{FQ}` WHERE CAST({inv_col} AS STRING) IN ({in_list})"])
            existing = {(x["i"], x["t"]) for x in json.loads(out or "[]")}
    new_rows = [r for r in rows
                if not (inv_col and trk_col and (str(r.get(inv_col)), str(r.get(trk_col))) in existing)]

    print(f"Target table : {FQ}")
    print(f"Mapped cols  : {len(mapped_cols)} of {len(cols)} table columns")
    unfilled = sorted(set(cols) - mapped_cols - PROVENANCE)
    if unfilled:
        print(f"Left NULL    : {unfilled}")
    if unmapped_csv:
        print(f"CSV-only cols (not in table, ignored): {len(unmapped_csv)}")
    print(f"Dedupe key   : {inv_col} + {trk_col}" if inv_col and trk_col else
          "Dedupe key   : NONE FOUND - re-runs could double-load; check schema")
    print("\nInvoice              Rows     Net Charge")
    for inv, (n, amt) in sorted(per_invoice.items()):
        print(f"  {inv:<18}{n:>6}  ${amt:>12,.2f}")
    total = sum(v[1] for v in per_invoice.values())
    print(f"  {'TOTAL':<18}{len(rows):>6}  ${total:>12,.2f}")
    print(f"\nAlready in table: {len(rows) - len(new_rows)}   New rows to append: {len(new_rows)}")

    if not a.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply after confirming the totals.")
        return
    if not new_rows:
        print("Nothing new to load.")
        return

    stamp = datetime.now(timezone.utc).isoformat()
    for r in new_rows:
        if "loaded_at" in cols:
            r["loaded_at"] = stamp
        if "loaded_by" in cols:
            r["loaded_by"] = SA
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as tmp:
        for r in new_rows:
            tmp.write(json.dumps(r) + "\n")
    bq(["load", "--source_format=NEWLINE_DELIMITED_JSON", "--noreplace",
        f"{PROJECT}:{DATASET}.{TABLE}", tmp.name], capture=False)
    os.unlink(tmp.name)
    print(f"Loaded {len(new_rows)} rows into {FQ}.")


if __name__ == "__main__":
    main()

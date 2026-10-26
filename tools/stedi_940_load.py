#!/usr/bin/env python3
"""Load Stedi 940 warehouse shipping orders into `americanflat.warehouse.shipments_940_raw`.

Grain: one row per 940 transaction (W01 line items nested in `lines`). The 945
ship confirmations are already in BigQuery; the 940s were not, so an order the
warehouse never confirmed had no marketplace anywhere in the warehouse. Every
940 names its marketplace in N9*IT (ACNDA_TGTPL, AMAZON.COM, WAYFAIR LLC,
SHOPIFY, WALMARTCOM, MACYS, ...); `warehouse.shipments_940` maps that to the
channel codes `warehouse.channel_keys` uses.

Two modes:

    python3 tools/stedi_940_load.py fetch --since 2026-09-01 [--out stedi_940_rows.jsonl]
        Pull every outbound 940 to the warehouse partnerships processed since
        the date from the Stedi Core API and write newline-delimited JSON.
        Read-only against Stedi, touches nothing in BigQuery.

    python3 tools/stedi_940_load.py load [--since 2026-09-01 | --from-file rows.jsonl]
        Fetch (or take a file from `fetch`), stage it and MERGE on transactionId,
        so re-running a window updates in place and never duplicates. Without
        --since it resumes from the table's newest processedAt minus a day.
        Writes with your own gcloud credentials; it never creates the table
        (schemas/shipments_940_raw.sql does, once).

Stedi key: STEDI_API_KEY, or STEDI_API_KEY_FILE pointing at a file holding the
bare key or an "API Key: <key>" line. Sent only as the Authorization header.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / "schemas" / "shipments_940_raw.json"

PROJECT = "americanflat"
TABLE = "warehouse.shipments_940_raw"
STAGE = "warehouse.shipments_940_stage"

API = "https://core.us.stedi.com/2023-08-01"
PARTNERSHIPS = {  # 940 partnership -> warehouse code used by warehouse.shipments_945
    "americanflat_taylored": "FON",
    "americanflat_Taylored-EAST_NJ": "NJ",
    "americanflat_Taylored-SC": "SC",
    "americanflat-vc_yusen-ben": "NL",
}


# --------------------------------------------------------------------- Stedi

def stedi_key():
    key = os.environ.get("STEDI_API_KEY")
    if key:
        return key
    path = os.environ.get("STEDI_API_KEY_FILE")
    if path:
        txt = Path(os.path.expanduser(path)).read_text(encoding="utf-8-sig").strip()
        m = re.search(r"API Key:\s*(\S+)", txt)
        if m:
            return m.group(1)
        if txt and len(txt.split()) == 1:
            return txt
    sys.exit("Stedi API key not configured: set STEDI_API_KEY or STEDI_API_KEY_FILE.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def api(path, key, tries=6):
    """The partnership-filtered list 504s now and then; back off and retry."""
    for n in range(tries):
        try:
            req = urllib.request.Request(f"{API}/{path}", headers={"Authorization": key})
            return json.loads(urllib.request.urlopen(req, timeout=90).read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            code = getattr(e, "code", None)
            if n == tries - 1 or (code and code not in (429, 500, 502, 503, 504)):
                raise
            time.sleep(2 ** n * 3)


def artifact(url, key, tries=5):
    """Artifact URLs 302 to presigned S3, which 400s if the auth header is
    forwarded - so follow the redirect by hand. Retries cover truncated reads."""
    for n in range(tries):
        try:
            try:
                return _opener.open(urllib.request.Request(url, headers={"Authorization": key}),
                                    timeout=60).read()
            except urllib.error.HTTPError as e:
                if e.code not in (301, 302, 303, 307) or not e.headers.get("Location"):
                    raise
                return urllib.request.urlopen(e.headers["Location"], timeout=60).read()
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(2 ** n)


def list_940s(key, since):
    """Outbound 940 transaction summaries per warehouse partnership, newest first,
    back to `since` (UTC datetime)."""
    out = []
    for pid in PARTNERSHIPS:
        tok, n = None, 0
        while True:
            d = api(f"transactions?pageSize=100&direction=OUTBOUND&partnershipId={pid}"
                    + (f"&pageToken={tok}" if tok else ""), key)
            items = d.get("items", [])
            fresh = [x for x in items if parse_ts(x["processedAt"]) >= since]
            for x in fresh:
                ts = (((x.get("x12") or {}).get("metadata") or {}).get("transaction") or {})
                if str(ts.get("transactionSetIdentifier")) == "940":
                    out.append(x)
                    n += 1
            tok = d.get("nextPageToken")
            if not tok or len(fresh) < len(items):
                break
        print(f"  {pid}: {n} 940s", file=sys.stderr, flush=True)
    return out


# ------------------------------------------------------------------- parsing

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def as_list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def n9_map(section):
    """{qualifier: value} for every N9 in a heading / line, first one wins."""
    out = {}
    for k, v in section.items():
        if k.startswith("reference_identification_N9"):
            for r in as_list(v):
                q = r.get("reference_identification_qualifier_01")
                if q and q not in out:
                    out[q] = str(r.get("reference_identification_02", ""))
    return out


def date_or_none(s):
    s = str(s or "")
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def parse_940(doc, summary):
    head = doc.get("heading", {})
    w05 = head.get("shipping_order_identification_W05", {})
    n9 = n9_map(head)

    st = {}
    for loop in as_list(head.get("name_N1_loop")) + as_list(head.get("name_N1_loop_sold_to_and_ship_to")):
        if (loop.get("name_N1") or {}).get("entity_identifier_code_01") == "ST":
            n3 = as_list(loop.get("address_information_N3"))
            n4 = loop.get("geographic_location_N4") or {}
            st = {"shipToName": loop["name_N1"].get("name_02"),
                  "shipToAddress1": (n3[0].get("address_information_01") if n3 else None),
                  "shipToCity": n4.get("city_name_01"),
                  "shipToState": n4.get("state_or_province_code_02"),
                  "shipToPostalCode": n4.get("postal_code_03"),
                  "shipToCountry": n4.get("country_code_04")}
            break

    g62 = {}
    for k, v in head.items():
        if k.startswith("date_time_G62"):
            for r in as_list(v):
                g62.setdefault(r.get("date_qualifier_01"), r.get("date_02"))

    w66 = head.get("warehouse_carrier_information_W66") or {}

    lines = []
    for lx in as_list((doc.get("detail") or {}).get("assigned_number_LX_loop")):
        seq = (lx.get("assigned_number_LX") or {}).get("assigned_number_01")
        for w in as_list(lx.get("line_item_detail_warehouse_W01_loop")):
            w01 = w.get("line_item_detail_warehouse_W01") or {}
            ids = {w01.get(f"product_service_id_qualifier_{i:02d}"): w01.get(f"product_service_id_{i + 1:02d}")
                   for i in range(4, 20, 2) if w01.get(f"product_service_id_qualifier_{i:02d}")}
            g69 = as_list(w.get("line_item_detail_description_G69"))
            amt = (w.get("monetary_amount_AMT") or {}).get("monetary_amount_02")
            lines.append({
                "lineSeq": seq,
                "sku": ids.get("VN"),
                "customerItemNumber": ids.get("CN"),
                "upc": w01.get("upc_case_code_03") or ids.get("UP"),
                "quantityOrdered": w01.get("quantity_ordered_01"),
                "uom": w01.get("unit_or_basis_for_measurement_code_02"),
                "description": g69[0].get("free_form_description_01") if g69 else None,
                "lineItemRef": n9_map(w).get("LI"),
                "amount": str(amt) if amt is not None else None,
            })

    pid = (summary.get("partnership") or {}).get("partnershipId", "")
    return {
        "transactionId": summary["transactionId"],
        "fileExecutionId": summary.get("fileExecutionId"),
        "processedAt": summary["processedAt"],
        "partnershipId": pid,
        "warehouse": PARTNERSHIPS.get(pid),
        "orderStatusCode": w05.get("order_status_code_01"),
        "depositorOrderNumber": str(w05.get("depositor_order_number_02") or "") or None,
        "purchaseOrderNumber": str(w05.get("purchase_order_number_03") or "") or None,
        "marketplaceRaw": n9.get("IT"),
        "department": n9.get("DP"),
        "internalVendorNumber": n9.get("IA"),
        "customerOrderNumber": n9.get("CO"),
        "dataSource": n9.get("S2"),
        "n9": n9,
        **st,
        "orderedDate": date_or_none(g62.get("52")),
        "requestedShipDate": date_or_none(g62.get("10")),
        "cancelAfterDate": date_or_none(g62.get("01")),
        "carrierScac": w66.get("standard_carrier_alpha_code_10"),
        "routing": w66.get("routing_05"),
        "transportMethod": w66.get("transportation_method_type_code_02"),
        "paymentMethod": w66.get("shipment_method_of_payment_01"),
        "lines": lines,
    }


def fetch(since, key, ingested_by):
    print(f"listing 940s since {since:%Y-%m-%d %H:%M} UTC", file=sys.stderr, flush=True)
    summaries = list_940s(key, since)

    def one(x):
        url = next((a["url"] for a in x.get("artifacts", []) if a.get("artifactType") == "application/json"), None)
        if not url:
            return None, x["transactionId"]
        return parse_940(json.loads(artifact(url, key)), x), None

    rows, missing = {}, []
    now = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(8) as ex:
        for i, (row, miss) in enumerate(ex.map(one, summaries), 1):
            if row:
                row["_ingested_at"], row["_ingested_by"] = now, ingested_by
                rows[row["transactionId"]] = row
            else:
                missing.append(miss)
            if i % 1000 == 0:
                print(f"  parsed {i}/{len(summaries)}", file=sys.stderr, flush=True)
    if missing:
        print(f"  {len(missing)} 940s had no JSON artifact and were skipped", file=sys.stderr)
    return list(rows.values())


def summarize(rows):
    by = {}
    for r in rows:
        k = (r["warehouse"], r["marketplaceRaw"] or "(none)")
        by[k] = by.get(k, 0) + 1
    print(f"{len(rows)} 940s")
    for (wh, mk), n in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {wh or '?':<4} {mk:<16} {n}")


# ---------------------------------------------------------------- BigQuery

def bq(args, capture=True):
    exe = shutil.which("bq")
    if not exe:
        sys.exit("bq CLI not found. Install the Google Cloud SDK and run `gcloud auth login`.")
    env = dict(os.environ)
    env.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
    return subprocess.run([exe, f"--project_id={PROJECT}", "--location=US"] + args,
                          capture_output=capture, text=True, env=env)


def gcloud_account():
    exe = shutil.which("gcloud")
    if not exe:
        return "unknown"
    p = subprocess.run([exe, "config", "get-value", "account"], capture_output=True, text=True)
    return (p.stdout or "").strip() or "unknown"


def table_cutoff():
    p = bq(["query", "--use_legacy_sql=false", "--format=json",
            f"SELECT CAST(MAX(processedAt) AS STRING) AS p FROM `{PROJECT}.{TABLE}`"])
    if p.returncode:
        err = (p.stderr or p.stdout).strip()
        if "not found" in err.lower():
            sys.exit(f"{PROJECT}.{TABLE} does not exist yet. Create it once with "
                     f"schemas/shipments_940_raw.sql (BigQuery console), then rerun.")
        sys.exit(f"Could not read {TABLE}:\n{err}")
    val = (json.loads(p.stdout or "[]") or [{}])[0].get("p")
    return parse_ts(val.replace(" ", "T").replace("+00", "+00:00")) if val else None


def cmd_load(rows):
    if not rows:
        print("nothing to load")
        return 0
    tmp = Path(tempfile.mkdtemp(prefix="stedi-940-"))
    ndjson = tmp / "rows.jsonl"
    write_ndjson(rows, ndjson)

    p = bq(["load", "--source_format=NEWLINE_DELIMITED_JSON", "--replace",
            STAGE, str(ndjson), str(SCHEMA_PATH)])
    if p.returncode:
        sys.exit(f"Staging load failed (target untouched):\n{(p.stderr or p.stdout).strip()}")
    print(f"staged {len(rows)} 940s in {STAGE}")

    cols = [f["name"] for f in json.loads(SCHEMA_PATH.read_text())]
    sets = ", ".join(f"{c} = S.{c}" for c in cols if c != "transactionId")
    merge = (f"MERGE `{PROJECT}.{TABLE}` T USING `{PROJECT}.{STAGE}` S\n"
             f"ON T.transactionId = S.transactionId\n"
             f"WHEN MATCHED THEN UPDATE SET {sets}\n"
             f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) "
             f"VALUES ({', '.join('S.' + c for c in cols)})")
    p = bq(["query", "--use_legacy_sql=false", merge])
    if p.returncode:
        sys.exit(f"MERGE failed (target unchanged):\n{(p.stderr or p.stdout).strip()}")
    bq(["rm", "-f", "-t", STAGE])

    p = bq(["query", "--use_legacy_sql=false", "--format=csv",
            f"SELECT COUNT(*) AS row_count, COUNT(DISTINCT transactionId) AS tx_count, "
            f"CAST(MAX(processedAt) AS STRING) AS newest FROM `{PROJECT}.{TABLE}`"])
    print(p.stdout.strip())
    lines = [l for l in p.stdout.strip().splitlines() if l]
    if len(lines) >= 2:
        n_rows, n_tx = lines[-1].split(",")[:2]
        if n_rows != n_tx:
            print("INTEGRITY FAILURE: rows != distinct transactionIds", file=sys.stderr)
            return 1
    print(f"merged into {PROJECT}.{TABLE}")
    return 0


def write_ndjson(rows, path):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--since", required=True, help="YYYY-MM-DD (UTC)")
    f.add_argument("--out", default="stedi_940_rows.jsonl")
    ld = sub.add_parser("load")
    ld.add_argument("--since", help="YYYY-MM-DD (UTC); default: table's newest processedAt minus 1 day")
    ld.add_argument("--from-file", help="NDJSON written by `fetch`, instead of fetching")
    args = ap.parse_args()

    if args.mode == "fetch":
        rows = fetch(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc), stedi_key(), "fetch")
        write_ndjson(rows, args.out)
        summarize(rows)
        print(f"wrote {args.out}")
        return 0

    who = gcloud_account()
    print(f"writing as {who}")
    if args.from_file:
        rows = [json.loads(l) for l in open(args.from_file) if l.strip()]
        for r in rows:
            r["_ingested_at"], r["_ingested_by"] = datetime.now(timezone.utc).isoformat(), who
    else:
        if args.since:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        else:
            cut = table_cutoff()
            if not cut:
                sys.exit("Table is empty: pass --since YYYY-MM-DD for the first load.")
            since = cut - timedelta(days=1)
        rows = fetch(since, stedi_key(), who)
    summarize(rows)
    return cmd_load(rows)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Load weekly FedEx and UPS invoice files into BigQuery (americanflat.finance).

  * FedEx invoice CSV           -> finance.fedex_shipping_costs  (one row per invoice line)
  * UPS billing-data CSV (v2.1) -> finance.ups_shipping_costs    (one row per invoice line)

Each carrier has its own load log (finance.fedex_shipping_costs_load_log,
finance.ups_shipping_costs_load_log) recording every file loaded. Stamps.com
print history is NOT handled here — it loads to finance.stamps_shipping_costs
via tools/stamps_shipping_costs_load.py.

Re-running is safe:
  * A file whose SHA-256 is already in the load log is skipped (use --force to reprocess).
  * Lines are keyed on a hash of the line, so the same invoice exported twice
    never double-counts. For FedEx the key ignores the order of the repeated
    charge and commodity column pairs, which FedEx shuffles between exports.
  * An invoice that's already loaded never gains lines. If a file brings lines
    the table doesn't recognize for an invoice already there, that file is
    refused before anything is written (override: --allow-invoice-overlap).

Before writing, each invoice's line sum must equal the invoice total printed
in the file. A mismatch refuses that file (override: --allow-mismatch).

Auth: uses whatever account `gcloud auth login` is signed in as (no service
account hop). Writes nothing with --dry-run.

Usage:
  python3 tools/carrier_invoices_load.py --dry-run FILE [FILE ...]   # parse + checks only
  python3 tools/carrier_invoices_load.py --init                      # one-time: create missing tables
  python3 tools/carrier_invoices_load.py FILE [FILE ...]             # load

Exit codes: 0 ok · 2 bad usage / unreadable file · 3 total check failed or invoice overlap ·
            4 bq/gcloud missing · 5 BigQuery error · 6 post-load verification failed
"""
import argparse, csv, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_DIR = HERE.parent / "schemas"
TABLES = {
    "fedex": "fedex_shipping_costs",
    "ups": "ups_shipping_costs",
}
LOG_TABLES = {
    "fedex": "fedex_shipping_costs_load_log",
    "ups": "ups_shipping_costs_load_log",
}
SCHEMA_FILE = {
    "fedex_shipping_costs": "fedex_shipping_costs.json",
    "ups_shipping_costs": "ups_shipping_costs.json",
    "fedex_shipping_costs_load_log": "carrier_invoice_load_log.json",
    "ups_shipping_costs_load_log": "carrier_invoice_load_log.json",
}
AMOUNT_COL = {"fedex": "net_charge", "ups": "net_amount"}
PARTITION = {"fedex": "invoice_date", "ups": "invoice_date"}
CLUSTER = {"fedex": "invoice_number,tracking_id", "ups": "invoice_number,tracking_number"}


# ---------------------------------------------------------------- helpers
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(v):
    """Strip whitespace and Excel ="..." wrappers; '' -> None."""
    if v is None:
        return None
    s = str(v).strip()
    m = re.fullmatch(r'="?(.*?)"?', s)
    if m and s.startswith("="):
        s = m.group(1).strip()
    return s or None


def money(v):
    s = clean(v)
    if s is None:
        return None
    s = s.replace("$", "").replace(",", "")
    try:
        return str(Decimal(s).quantize(Decimal("0.01")))
    except InvalidOperation:
        return None


def num(v):
    s = clean(v)
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def intval(v):
    f = num(v)
    return int(f) if f is not None else None


def date_any(v):
    s = clean(v)
    if not s:
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def to_lbs(amount, unit):
    a = num(amount)
    if a is None:
        return None
    return round(a * 2.20462, 3) if (clean(unit) or "").lower().startswith("k") else a


def line_keys(rows_raw):
    """Stable key per line: hash of the full raw line + occurrence index
    (so two byte-identical lines inside one invoice both survive)."""
    seen, keys = {}, []
    for raw in rows_raw:
        h = hashlib.sha256("\x1f".join("" if x is None else str(x) for x in raw).encode()).hexdigest()
        n = seen.get(h, 0)
        seen[h] = n + 1
        keys.append(f"{h[:40]}#{n}")
    return keys


# FedEx repeats these column pairs (51 charge pairs, 4 commodity pairs) and
# doesn't keep their order stable between exports: the same shipment can list
# Fuel Surcharge before Discount in one download and after it in another. So
# the key treats each pair group as an unordered set. (Found 2026-10-19: two
# overlapping Jan–Sep exports disagreed on 847 June lines, all the same money.)
FEDEX_PAIRS = [
    ("Tracking ID Charge Description", "Tracking ID Charge Amount"),
    ("Commodity Description", "Commodity Country/Territory Code"),
]


def fedex_line_keys(head, body):
    """Stable key per FedEx line that ignores the order of repeated pairs,
    + occurrence index (so two identical lines inside one invoice both survive)."""
    paired = {n for pair in FEDEX_PAIRS for n in pair}
    seen, keys = {}, []
    for r in body:
        cell = lambda i: clean(r[i]) if i < len(r) else None
        fixed = sorted((h, cell(i)) for i, h in enumerate(head)
                       if h not in paired and cell(i) is not None)
        groups = {}
        for a, b in FEDEX_PAIRS:
            ia = [i for i, h in enumerate(head) if h == a]
            ib = [i for i, h in enumerate(head) if h == b]
            norm_b = money if "Amount" in b else (lambda v: clean(v))
            groups[a] = sorted([cell(x), norm_b(r[y] if y < len(r) else None)]
                               for x, y in zip(ia, ib)
                               if cell(x) is not None or cell(y) is not None)
        canon = json.dumps({"fixed": fixed, "pairs": groups}, separators=(",", ":"))
        h = hashlib.sha256(canon.encode()).hexdigest()
        n = seen.get(h, 0)
        seen[h] = n + 1
        keys.append(f"{h[:40]}#{n}")
    return keys


# ---------------------------------------------------------------- detection
def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return list(csv.reader(f))


def detect(rows):
    if not rows:
        return None
    head = [c.strip() for c in rows[0]]
    if "Express or Ground Tracking ID" in head and "Net Charge Amount" in head:
        return "fedex"
    if head and head[0] in ("2.1", "2.0") and len(head) >= 200:
        return "ups"
    return None


# ---------------------------------------------------------------- parsers
def parse_fedex(rows):
    head = [c.strip() for c in rows[0]]
    # Duplicate header names (charge description/amount pairs) -> make unique.
    names, cnt = [], {}
    for h in head:
        k = cnt.get(h, 0)
        cnt[h] = k + 1
        names.append(h if k == 0 else f"{h}.{k}")
    body = [r for r in rows[1:] if any(c.strip() for c in r)]
    keys = fedex_line_keys(head, body)
    out, totals = [], {}
    for r, key in zip(body, keys):
        d = {n: clean(r[i]) if i < len(r) else None for i, n in enumerate(names)}
        inv = d.get("Invoice Number")
        rec = {
            "row_key": key,
            "invoice_number": inv,
            "invoice_date": date_any(d.get("Invoice Date")),
            "bill_to_account": d.get("Bill to Account Number"),
            "invoice_amount_due": money(d.get("Original Amount Due")),
            "tracking_id": d.get("Express or Ground Tracking ID"),
            "shipment_date": date_any(d.get("Shipment Date")),
            "service_type": d.get("Service Type"),
            "transportation_charge": money(d.get("Transportation Charge Amount")),
            "net_charge": money(d.get("Net Charge Amount")),
            "rated_weight_lbs": to_lbs(d.get("Rated Weight Amount"), d.get("Rated Weight Units")),
            "actual_weight_lbs": to_lbs(d.get("Actual Weight Amount"), d.get("Actual Weight Units")),
            "pieces": intval(d.get("Number of Pieces")),
            "zone": d.get("Zone Code"),
            "shipper_city": d.get("Shipper City"),
            "shipper_state": d.get("Shipper State"),
            "recipient_state": d.get("Recipient State"),
            "recipient_zip": d.get("Recipient Zip Code"),
            "ref1": d.get("Original Customer Reference"),
            "ref2": d.get("Original Ref#2"),
            "ref3_po": d.get("Original Ref#3/PO Number"),
            "raw": {k: v for k, v in d.items() if v is not None},
        }
        out.append(rec)
        t = totals.setdefault(inv, {"lines": 0, "sum": Decimal("0"), "stated": set(),
                                    "invoice_date": rec["invoice_date"]})
        t["lines"] += 1
        t["sum"] += Decimal(rec["net_charge"] or "0")
        if rec["invoice_amount_due"] is not None:
            t["stated"].add(Decimal(rec["invoice_amount_due"]))
    return out, totals


# UPS billing data v2.1 — 0-based column positions used for typed fields.
# The whole line is also kept in `raw` as c000..cNNN.
UPS_COLS = {
    "account_number": 1, "invoice_date": 4, "invoice_number": 5, "invoice_amount": 10,
    "transaction_date": 11, "lead_shipment_number": 13, "package_quantity": 18,
    "tracking_number": 20, "ref1": 21, "ref2": 22, "entered_weight": 26,
    "billed_weight": 28, "charge_category_code": 43, "charge_description": 45,
    "incentive_amount": 51, "net_amount": 52,
    "receiver_name": 74, "receiver_city": 78, "receiver_state": 79, "receiver_zip": 80,
}


def parse_ups(rows):
    body = [r for r in rows if r and any(c.strip() for c in r)]
    keys = line_keys(body)
    out, totals = [], {}
    g = lambda r, i: clean(r[i]) if i < len(r) else None
    for r, key in zip(body, keys):
        c = {n: g(r, i) for n, i in UPS_COLS.items()}
        rec = {
            "row_key": key,
            "invoice_number": c["invoice_number"],
            "invoice_date": date_any(c["invoice_date"]),
            "account_number": c["account_number"],
            "invoice_amount": money(c["invoice_amount"]),
            "transaction_date": date_any(c["transaction_date"]),
            "lead_shipment_number": c["lead_shipment_number"],
            "tracking_number": c["tracking_number"],
            "package_quantity": intval(c["package_quantity"]),
            "ref1": c["ref1"],
            "ref2": c["ref2"],
            "entered_weight_lbs": num(c["entered_weight"]),
            "billed_weight_lbs": num(c["billed_weight"]),
            "charge_category_code": c["charge_category_code"],
            "charge_description": c["charge_description"],
            "incentive_amount": money(c["incentive_amount"]),
            "net_amount": money(c["net_amount"]),
            "receiver_name": c["receiver_name"],
            "receiver_city": c["receiver_city"],
            "receiver_state": c["receiver_state"],
            "receiver_zip": c["receiver_zip"],
            "raw": {f"c{i:03d}": clean(v) for i, v in enumerate(r) if clean(v) is not None},
        }
        out.append(rec)
        inv = rec["invoice_number"]
        t = totals.setdefault(inv, {"lines": 0, "sum": Decimal("0"), "stated": set(),
                                    "invoice_date": rec["invoice_date"]})
        t["lines"] += 1
        t["sum"] += Decimal(rec["net_amount"] or "0")
        if rec["invoice_amount"] is not None:
            t["stated"].add(Decimal(rec["invoice_amount"]))
    return out, totals


# ---------------------------------------------------------------- bq plumbing
class BQ:
    def __init__(self, project, dataset, location):
        self.project, self.dataset, self.location = project, dataset, location

    def fq(self, table):
        return f"`{self.project}.{self.dataset}.{table}`"

    def ref(self, table):
        return f"{self.project}:{self.dataset}.{table}"

    def run(self, args, check=True):
        cmd = ["bq", f"--project_id={self.project}", f"--location={self.location}", "--quiet"] + args
        p = subprocess.run(cmd, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"bq {' '.join(args[:2])} failed:\n{(p.stderr or p.stdout).strip()[:1500]}")
        return p

    def query(self, sql):
        p = self.run(["query", "--use_legacy_sql=false", "--format=json", "--max_rows=10000", sql])
        out = p.stdout.strip()
        try:
            return json.loads(out) if out.startswith("[") else []
        except json.JSONDecodeError:
            return []

    def exists(self, table):
        return self.run(["show", self.ref(table)], check=False).returncode == 0


def ensure_tools():
    missing = [t for t in ("bq", "gcloud") if not shutil.which(t)]
    if missing:
        print(f"ERROR: {', '.join(missing)} not found. Install the Google Cloud CLI "
              "(brew install --cask google-cloud-sdk), then run `gcloud auth login`.")
        sys.exit(4)
    acct = subprocess.run(["gcloud", "config", "get-value", "account"],
                          capture_output=True, text=True).stdout.strip()
    if not acct or acct == "(unset)":
        print("ERROR: no gcloud account. Run `gcloud auth login` once, then retry.")
        sys.exit(4)
    return acct


def init(bq):
    """Create any missing carrier/log tables. Never creates the dataset itself."""
    for kind, table in TABLES.items():
        if bq.exists(table):
            print(f"Exists: {table}")
        else:
            bq.run(["mk", "--table", f"--time_partitioning_field={PARTITION[kind]}",
                    "--time_partitioning_type=MONTH", f"--clustering_fields={CLUSTER[kind]}",
                    bq.ref(table), str(SCHEMA_DIR / SCHEMA_FILE[table])])
            print(f"Created table {table}")
        log = LOG_TABLES[kind]
        if bq.exists(log):
            print(f"Exists: {log}")
        else:
            bq.run(["mk", "--table", bq.ref(log), str(SCHEMA_DIR / SCHEMA_FILE[log])])
            print(f"Created table {log}")


class InvoiceOverlap(Exception):
    pass


def load_file(bq, kind, recs, meta, tmpdir, allow_overlap=False):
    table = TABLES[kind]
    stg = f"_stg_{kind}_{os.getpid()}"
    nd = Path(tmpdir) / f"{stg}.ndjson"
    with open(nd, "w") as f:
        for r in recs:
            f.write(json.dumps({**r, **meta}, separators=(",", ":")) + "\n")
    try:
        bq.run(["load", "--replace", "--source_format=NEWLINE_DELIMITED_JSON",
                bq.ref(stg), str(nd), str(SCHEMA_DIR / SCHEMA_FILE[table])])
        bq.run(["update", "--expiration=3600", bq.ref(stg)], check=False)
        pre = bq.query(f"SELECT COUNTIF(T.row_key IS NULL) AS new_rows, "
                       f"COUNTIF(T.row_key IS NOT NULL) AS existing_rows "
                       f"FROM {bq.fq(stg)} S LEFT JOIN {bq.fq(table)} T USING (row_key)")
        new_rows = int(pre[0]["new_rows"]) if pre else None
        existing = int(pre[0]["existing_rows"]) if pre else None
        # An issued invoice never gains lines. New rows for an invoice that's
        # already loaded mean the line key didn't recognize a re-export, and
        # merging would double-count it — stop before writing anything.
        if new_rows and not allow_overlap:
            clash = bq.query(
                f"SELECT S.invoice_number, COUNT(*) AS n FROM {bq.fq(stg)} S "
                f"LEFT JOIN {bq.fq(table)} T USING (row_key) "
                f"WHERE T.row_key IS NULL AND S.invoice_number IN "
                f"(SELECT DISTINCT invoice_number FROM {bq.fq(table)}) "
                f"GROUP BY 1 ORDER BY 1")
            if clash:
                raise InvoiceOverlap(", ".join(f"{c['invoice_number']} ({c['n']} lines)" for c in clash))
        bq.query(f"MERGE {bq.fq(table)} T USING {bq.fq(stg)} S ON T.row_key = S.row_key "
                 f"WHEN NOT MATCHED THEN INSERT ROW")
        amt = AMOUNT_COL[kind]
        post = bq.query(f"SELECT COUNT(*) AS n, CAST(SUM(T.{amt}) AS STRING) AS amt "
                        f"FROM {bq.fq(stg)} S JOIN {bq.fq(table)} T USING (row_key)")
        n_in = int(post[0]["n"]) if post else -1
        amt_in = Decimal(post[0]["amt"] or "0") if post else Decimal("-1")
        return new_rows, existing, n_in, amt_in
    finally:
        # Only ever removes this run's own scratch table; it also self-expires in an hour.
        bq.run(["rm", "-f", "-t", bq.ref(stg)], check=False)


def log_load(bq, kind, row):
    cols = ", ".join(row)
    vals = ", ".join("NULL" if v is None else
                     (f"NUMERIC '{v}'" if isinstance(v, Decimal) else
                      str(v) if isinstance(v, (int, float)) else
                      ("CURRENT_TIMESTAMP()" if v == "__NOW__" else
                       "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"))
                     for v in row.values())
    bq.query(f"INSERT INTO {bq.fq(LOG_TABLES[kind])} ({cols}) VALUES ({vals})")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--project", default="americanflat")
    ap.add_argument("--dataset", default="finance")
    ap.add_argument("--location", default="US")
    ap.add_argument("--init", action="store_true", help="create missing carrier and log tables")
    ap.add_argument("--dry-run", action="store_true", help="parse and check only; no BigQuery calls")
    ap.add_argument("--force", action="store_true", help="reprocess files already in the load log")
    ap.add_argument("--allow-mismatch", action="store_true", help="load even if line sum != invoice total")
    ap.add_argument("--allow-invoice-overlap", action="store_true",
                    help="load new lines for invoices already in the table (normally refused)")
    ap.add_argument("--ndjson-out", help="dry-run: also write the NDJSON payloads to this folder")
    a = ap.parse_args()

    if not a.files and not a.init:
        ap.error("give at least one file, or --init")

    # ---- parse + check everything first, so nothing is written on a bad batch
    parsed, rc = [], 0
    for p in a.files:
        if not os.path.isfile(p):
            print(f"ERROR: not a file: {p}")
            sys.exit(2)
        rows = read_rows(p)
        kind = detect(rows)
        name = Path(p).name
        if kind not in TABLES:
            print(f"SKIP  {name}: not a FedEx invoice or UPS billing-data file")
            continue
        recs, totals = parse_fedex(rows) if kind == "fedex" else parse_ups(rows)
        ok = True
        print(f"\n{kind.upper():6} {name}  ({len(recs)} rows)")
        for inv, t in sorted(totals.items(), key=lambda x: str(x[0])):
            stated = sorted(t["stated"])
            tie = len(stated) == 1 and stated[0] == t["sum"]
            flag = "ties" if tie else f"MISMATCH (invoice says {', '.join(f'${s:,.2f}' for s in stated) or 'nothing'})"
            if not tie:
                ok = False
            print(f"       {inv}  {t['invoice_date']}  {t['lines']:>5} lines  ${t['sum']:>12,.2f}  {flag}")
        print(f"       file total ${sum(t['sum'] for t in totals.values()):,.2f}")
        if not ok and not a.allow_mismatch:
            print("       -> REFUSED: line sum does not equal the invoice total (use --allow-mismatch to override)")
            rc = 3
            continue
        parsed.append((p, kind, recs, totals))

    if a.dry_run:
        if a.ndjson_out:
            os.makedirs(a.ndjson_out, exist_ok=True)
            for p, kind, recs, _ in parsed:
                out = Path(a.ndjson_out) / (Path(p).stem + ".ndjson")
                with open(out, "w") as f:
                    for r in recs:
                        f.write(json.dumps(r, separators=(",", ":")) + "\n")
        print(f"\nDRY RUN — {len(parsed)} file(s) ready; nothing written.")
        sys.exit(rc)

    acct = ensure_tools()
    bq = BQ(a.project, a.dataset, a.location)
    try:
        if a.init:
            init(bq)
        if not parsed:
            sys.exit(rc)
        kinds = sorted({k for _, k, _, _ in parsed})
        missing = [t for k in kinds for t in (TABLES[k], LOG_TABLES[k]) if not bq.exists(t)]
        if missing:
            print(f"ERROR: missing tables {missing}. Run once with --init.")
            sys.exit(5)

        done = {k: {r["source_sha256"] for r in bq.query(
                    f"SELECT DISTINCT source_sha256 FROM {bq.fq(LOG_TABLES[k])} WHERE status = 'loaded'")}
                for k in kinds}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmp:
            print("\nLoading to BigQuery as", acct)
            for p, kind, recs, totals in parsed:
                name, digest = Path(p).name, sha256_file(p)
                if digest in done[kind] and not a.force:
                    print(f"  {name}: already loaded (same file contents) — skipped")
                    continue
                meta = {"source_file": name, "source_sha256": digest, "loaded_at": now, "loaded_by": acct}
                try:
                    new_rows, existing, n_in, amt_in = load_file(bq, kind, recs, meta, tmp,
                                                                 a.allow_invoice_overlap)
                except InvoiceOverlap as e:
                    print(f"  {name}: REFUSED, nothing written. These invoices are already loaded "
                          f"but this file has lines the table doesn't recognize: {e}. "
                          f"Likely a re-export that would double-count. Override: --allow-invoice-overlap")
                    rc = 3
                    continue
                want_amt = sum(t["sum"] for t in totals.values())
                verified = (n_in == len(recs)) and amt_in == want_amt
                log_load(bq, kind, {
                    "loaded_at": "__NOW__", "loaded_by": acct, "source_file": name,
                    "source_sha256": digest, "carrier": kind,
                    "invoice_numbers": ",".join(str(i) for i in sorted(totals, key=str)),
                    "rows_in_file": len(recs), "rows_inserted": new_rows,
                    "rows_already_present": existing, "file_amount": Decimal(want_amt),
                    "status": "loaded" if verified else "verify_failed",
                })
                print(f"  {name}: {new_rows} new, {existing} already present"
                      f" · verified {n_in}/{len(recs)} rows, ${amt_in:,.2f} vs file ${want_amt:,.2f}"
                      + ("" if verified else "  <-- VERIFY FAILED"))
                if not verified:
                    rc = 6
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(5)
    sys.exit(rc)


if __name__ == "__main__":
    main()

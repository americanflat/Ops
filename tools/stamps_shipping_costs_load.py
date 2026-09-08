#!/usr/bin/env python3
"""Load Stamps.com shipping costs into `americanflat.finance.stamps_shipping_costs`.

Grain: one row per shipping label (one tracking number) out of a Stamps.com
PrintHistory export. `amount_paid` is what we actually paid for that label.

Two modes:

    python3 stamps_shipping_costs_load.py prepare <csv> [<csv> ...]
        Parse and normalize the CSVs, print a per-file and overall summary,
        and write newline-delimited JSON ready for `bq load`. Touches NOTHING
        in the cloud - it exists so a human can eyeball the totals first.

    python3 stamps_shipping_costs_load.py load <csv> [<csv> ...]
        Same parse, then load into BigQuery by IMPERSONATING the configured
        writer service account (no key files).

Re-running a file is safe. Each `source_file` owns its rows: the load appends
the new generation first, then removes that filename's older rows, so a
corrected re-export replaces its own data and never doubles it. Appending
first means a failed load deletes nothing. That is why `source_file` and
`ingested_at` are carried on every row.

This script never creates datasets, tables, service accounts, or IAM
bindings - that is the dataset owner's job (see docs/stamps-shipping-costs.md).
If the caller cannot impersonate the writer, the load fails with a message
naming who to ask; it cannot and will not grant itself access.

Config resolution (later wins): the defaults below, then a config.json beside
this file, then environment variables STAMPS_BQ_PROJECT / STAMPS_BQ_DATASET /
STAMPS_BQ_TABLE / STAMPS_BQ_SA / STAMPS_BQ_LOCATION.
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / "schemas" / "stamps_shipping_costs.json"

DEFAULTS = {
    "project_id": "americanflat",
    "dataset": "finance",
    "table": "stamps_shipping_costs",
    "impersonate_service_account": "invoice-writer@americanflat.iam.gserviceaccount.com",
    "location": "US",
}

ENV_KEYS = {
    "project_id": "STAMPS_BQ_PROJECT",
    "dataset": "STAMPS_BQ_DATASET",
    "table": "STAMPS_BQ_TABLE",
    "impersonate_service_account": "STAMPS_BQ_SA",
    "location": "STAMPS_BQ_LOCATION",
}

# Stamps.com renames these columns between the raw PrintHistory export and the
# consolidated "AMF Stamps.com Invoices" sheet, and the sheet drops several of
# them entirely. Match on a normalized header so both shapes load unchanged.
COLUMN_ALIASES = {
    "ship_date": ["ship date", "shipdate", "date", "print date", "ship date utc"],
    "tracking_number": ["tracking number", "tracking #", "tracking", "trackingnumber", "tracking no"],
    "carrier": ["carrier", "carrier name"],
    "service": ["service", "service used", "service type", "mail class"],
    "weight_lb": ["weight", "weight lb", "weight (lb)", "billed weight"],
    "amount_paid": ["amount paid", "amount", "total", "charge", "cost", "postage"],
    "adjusted_amount": ["adjusted amount", "adjustment", "adjusted"],
    "order_id": ["order id", "orderid", "order #", "order number"],
    "reference_1": ["reference 1", "reference1", "reference", "ref 1", "printed message"],
    "cost_code": ["cost code", "costcode"],
    "to_name": ["to name", "recipient", "recipient name", "ship to name"],
    "to_zip": ["to zip", "to zip code", "zip", "zip code", "postal code"],
}

MONEY_FIELDS = ("amount_paid", "adjusted_amount")


EXCEL_ESCAPE_RE = re.compile(r'^=\s*"(.*)"$', re.S)


def unescape_cell(value: str) -> str:
    """Undo the spreadsheet escaping Stamps.com applies to long numerics.

    USPS tracking numbers arrive as ``="9400150105800099724475"`` - the
    ``="..."`` formula wrapper that stops Excel rendering a 22-digit number in
    scientific notation and truncating it. Stored verbatim it is not a tracking
    number: it will not join to 3PL, FedEx or EDI data, and the escaped and
    bare forms of one label do not dedupe against each other. A leading
    apostrophe is the other form of the same trick.

    No legitimate value in this feed begins with ``="`` or ``'``, so this is
    safe to apply to every cell rather than guessing which columns carry it.
    """
    text = (value or "").strip()
    match = EXCEL_ESCAPE_RE.match(text)
    if match:
        # Excel doubles interior quotes inside the wrapper.
        text = match.group(1).replace('""', '"').strip()
    elif text.startswith("'"):
        text = text[1:].strip()
    return text


def normalize_header(name: str) -> str:
    """Fold a source header to its alias key: lowercase, punctuation to spaces."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def build_column_map(fieldnames: list) -> dict:
    """Map each source header to a schema column. Unrecognized headers are
    dropped rather than guessed at - a silently mismapped cost column is worse
    than a missing one."""
    lookup = {}
    for column, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            lookup.setdefault(alias, column)
    mapping = {}
    for raw in fieldnames or []:
        target = lookup.get(normalize_header(raw))
        # First header wins, so a real "Amount Paid" is never displaced by a
        # later generic "Amount".
        if target and target not in mapping.values():
            mapping[raw] = target
    return mapping


def parse_date(value: str):
    """Stamps.com exports MM/DD/YYYY; the sheet sometimes carries ISO already."""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_money(value: str):
    """Money as a string, keeping cents exact. Negatives are real: refunds and
    postage corrections post as negative rows, so they are preserved, not
    clamped to zero."""
    text = (value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        amount = -amount
    return str(amount)


WEIGHT_RE = re.compile(r"(?:(\d+(?:\.\d+)?)\s*lb)?\s*(?:(\d+(?:\.\d+)?)\s*oz)?", re.I)


def parse_weight(value: str):
    """Weight arrives either as decimal pounds or as 'Xlb Yoz'. Return pounds."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return str(Decimal(text))
    except InvalidOperation:
        pass
    match = WEIGHT_RE.match(text)
    if not match or not any(match.groups()):
        return None
    pounds = Decimal(match.group(1)) if match.group(1) else Decimal(0)
    if match.group(2):
        pounds += Decimal(match.group(2)) / Decimal(16)
    return str(pounds.quantize(Decimal("0.0001")))


def parse_csv(path: Path, ingested_at: str, ingested_by: str):
    """Turn one CSV into schema rows plus a report of what was skipped."""
    rows, skipped_no_tracking, skipped_no_amount = [], 0, 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        mapping = build_column_map(reader.fieldnames)
        if "tracking_number" not in mapping.values():
            raise SystemExit(
                f"{path.name}: no tracking-number column found in headers "
                f"{reader.fieldnames}. Cannot load without the natural key."
            )
        for raw_row in reader:
            row = {}
            for source, target in mapping.items():
                value = unescape_cell(raw_row.get(source))
                if target == "ship_date":
                    row[target] = parse_date(value)
                elif target in MONEY_FIELDS:
                    row[target] = parse_money(value)
                elif target == "weight_lb":
                    row[target] = parse_weight(value)
                else:
                    row[target] = value or None
            if not row.get("tracking_number"):
                skipped_no_tracking += 1
                continue
            # A label with no cost carries no financial signal and would skew
            # any cost-per-unit average toward zero.
            if row.get("amount_paid") is None:
                skipped_no_amount += 1
                continue
            row["source_file"] = path.name
            row["ingested_at"] = ingested_at
            row["ingested_by"] = ingested_by
            rows.append(row)
    return rows, {"no_tracking": skipped_no_tracking, "no_amount": skipped_no_amount}


def summarize(rows: list) -> dict:
    amounts = [Decimal(r["amount_paid"]) for r in rows if r.get("amount_paid") is not None]
    dates = sorted(r["ship_date"] for r in rows if r.get("ship_date"))
    carriers = {}
    for row in rows:
        key = row.get("carrier") or "(blank)"
        carriers[key] = carriers.get(key, 0) + 1
    return {
        "labels": len(rows),
        "total_cost": sum(amounts) if amounts else Decimal(0),
        "negative_rows": sum(1 for a in amounts if a < 0),
        "min_date": dates[0] if dates else None,
        "max_date": dates[-1] if dates else None,
        "carriers": carriers,
        "distinct_tracking": len({r["tracking_number"] for r in rows}),
    }


def print_summary(label: str, rows: list, skipped: dict = None) -> None:
    stats = summarize(rows)
    print(f"\n{label}")
    print(f"  labels                {stats['labels']}")
    print(f"  distinct tracking     {stats['distinct_tracking']}")
    print(f"  ship date range       {stats['min_date']} .. {stats['max_date']}")
    print(f"  total amount_paid     ${stats['total_cost']:,.2f}")
    print(f"  negative rows         {stats['negative_rows']} (refunds/corrections)")
    print(f"  carriers              " + ", ".join(
        f"{k}={v}" for k, v in sorted(stats["carriers"].items())))
    if skipped and (skipped["no_tracking"] or skipped["no_amount"]):
        print(f"  skipped               {skipped['no_tracking']} no tracking, "
              f"{skipped['no_amount']} no amount")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    local = HERE / "config.json"
    if local.exists():
        cfg.update(json.loads(local.read_text()))
    for key, env in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def resolve_exe(name: str) -> str:
    """On Windows bq/gcloud are .cmd shims that subprocess won't find by bare
    name; shutil.which honors PATHEXT on every platform."""
    return shutil.which(name) or name


def gcloud_account() -> str:
    try:
        out = subprocess.run(
            [resolve_exe("gcloud"), "config", "get-value", "account"],
            capture_output=True, text=True, timeout=30)
        return (out.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def require_sdk() -> None:
    """Fail early and legibly when the Google Cloud SDK is not installed -
    without it there is no impersonation and no load."""
    missing = [name for name in ("bq", "gcloud") if not shutil.which(name)]
    if missing:
        raise SystemExit(
            f"Google Cloud SDK not found on PATH (missing: {', '.join(missing)}).\n"
            f"`prepare` works without it; `load` does not. Install the SDK and run\n"
            f"`gcloud auth login`, or run `prepare` here and load from a machine that has it."
        )


def run_bq(args: list, cfg: dict) -> subprocess.CompletedProcess:
    """Impersonation is set per-subprocess so it never mutates the caller's
    ambient gcloud config. An empty service account means write with the
    caller's own credentials, skipping the impersonation hop entirely."""
    env = dict(os.environ)
    sa = cfg.get("impersonate_service_account") or ""
    if sa:
        env["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"] = sa
    else:
        # Inherited config could otherwise reintroduce impersonation.
        env.pop("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", None)
    cmd = [resolve_exe("bq"), f"--project_id={cfg['project_id']}",
           f"--location={cfg.get('location', 'US')}"] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def is_permission_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(s in lowered for s in (
        "permission", "access denied", "forbidden", "403", "not authorized"))


def permission_help(cfg: dict, raw: str) -> None:
    sa = cfg.get("impersonate_service_account") or ""
    table = f"{cfg['project_id']}.{cfg['dataset']}.{cfg['table']}"
    lowered = (raw or "").lower()

    # Distinguish "cannot impersonate" from "cannot write". They need opposite
    # fixes, and conflating them sends the operator chasing the wrong grant.
    if sa and ("impersonate" in lowered or "getaccesstoken" in lowered):
        print(
            f"\nCould not impersonate {sa}.\n\n"
            f"Authentication worked; the impersonation hop is what failed. Note that\n"
            f"the service account may not exist at all - the error cannot tell those\n"
            f"apart. Check with:\n\n"
            f"    gcloud iam service-accounts describe {sa}\n\n"
            f"If you can already write {cfg['dataset']} yourself (you can if you created\n"
            f"the table), skip the hop entirely:\n\n"
            f"    python3 {Path(__file__).name} load --no-impersonate <csv> ...\n\n"
            f"Otherwise ask an admin for roles/iam.serviceAccountTokenCreator on that\n"
            f"service account, or for the SA to be created per\n"
            f"docs/stamps-shipping-costs.md.\n\n"
            f"Raw error:\n{raw}", file=sys.stderr)
        return

    who = f"the writer {sa}" if sa else "your account"
    print(
        f"\nBigQuery refused the operation on {table}.\n\n"
        f"This script cannot grant itself access. Ask the dataset owner for whichever applies:\n"
        f"  * the table does not exist yet -> they create it once, per\n"
        f"    docs/stamps-shipping-costs.md\n"
        f"  * {who} cannot write the dataset -> WRITER on "
        f"{cfg['project_id']}:{cfg['dataset']}\n"
        f"    plus roles/bigquery.jobUser on the project\n\n"
        f"Raw error:\n{raw}", file=sys.stderr)


def total_of(rows: list) -> Decimal:
    return sum((Decimal(r["amount_paid"]) for r in rows
                if r.get("amount_paid") is not None), Decimal(0))


def dedupe(rows: list, keep_last: bool = True) -> tuple:
    """Collapse repeats of a tracking number, keeping the LAST occurrence.

    Stamps.com re-exports an overlapping date range under a new filename
    ("... (1).csv", "... (2).csv"), each a supserset of the last. Because those
    filenames differ, the per-source_file replacement cannot see them, and
    loading two of them would count the overlapping labels twice. Last-wins
    means the newest export you list on the command line supersedes earlier
    ones, so pass files in chronological order.
    """
    keep, dropped = {}, []
    for row in rows:
        key = row["tracking_number"]
        if key in keep:
            dropped.append((key, keep[key]["source_file"], row["source_file"]))
            if not keep_last:
                continue
        keep[key] = row
    return list(keep.values()), dropped


def cmd_prepare(paths: list, out_path: Path, ingested_at: str = None,
                report: dict = None) -> int:
    ingested_at = ingested_at or datetime.now(timezone.utc).isoformat()
    ingested_by = gcloud_account()
    everything = []
    for path in paths:
        rows, skipped = parse_csv(path, ingested_at, ingested_by)
        print_summary(f"{path.name}", rows, skipped)
        everything.extend(rows)
    if not everything:
        print("\nNo loadable rows found.", file=sys.stderr)
        return 1

    first_wins, _ = dedupe(everything, keep_last=False)
    everything, dropped = dedupe(everything)
    if dropped:
        cross = {(a, b) for _, a, b in dropped if a != b}
        print(f"\nDEDUPED {len(dropped)} repeated tracking number(s), keeping the "
              f"last occurrence.")
        for older, newer in sorted(cross):
            n = sum(1 for _, a, b in dropped if (a, b) == (older, newer))
            print(f"  {n:>6} label(s): {newer} supersedes {older}")

        # A shell glob expands ALPHABETICALLY, not by export date. A backfill
        # file covering a wide range sorts early (its range starts earliest)
        # while actually being the newest, most-adjusted export - so last-wins
        # can quietly replace post-audit amounts with stale ones. The operator
        # cannot see that from label counts, which stay identical; only the
        # money moves. So say what the money did.
        kept_total, alt_total = total_of(everything), total_of(first_wins)
        delta = kept_total - alt_total
        if report is not None:
            report["order_delta"] = delta
        print(f"\n  cost with last occurrence kept   ${kept_total:,.2f}  <- loading this")
        print(f"  cost with first occurrence kept  ${alt_total:,.2f}")
        if delta:
            print(f"  file order is worth ${abs(delta):,.2f} "
                  f"({'lower' if delta < 0 else 'higher'} as ordered)")
            print(f"  CHECK THIS. Files apply in the order given and the LAST "
                  f"occurrence of a\n"
                  f"  tracking number wins. A shell glob orders files "
                  f"alphabetically, by the date\n"
                  f"  range in the name - NOT by when they were exported. A "
                  f"wide backfill export\n"
                  f"  sorts early but is usually the newest and most adjusted, "
                  f"so a glob can\n"
                  f"  overwrite its post-audit amounts with stale weekly ones "
                  f"and understate cost.\n"
                  f"  If one file already covers the whole period, load just "
                  f"that file.")

    if len(paths) > 1:
        print_summary("TOTAL (deduped)", everything)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in everything:
            handle.write(json.dumps(row) + "\n")
    print(f"\nWrote {len(everything)} rows -> {out_path}")
    return 0


def cmd_load(paths: list, cfg: dict, accept_order: bool = False) -> int:
    require_sdk()
    table = f"{cfg['dataset']}.{cfg['table']}"
    fq_table = f"{cfg['project_id']}.{cfg['dataset']}.{cfg['table']}"

    sa = cfg.get("impersonate_service_account") or ""
    print(f"writing as {'impersonated ' + sa if sa else gcloud_account() + ' (no impersonation)'}")
    probe = run_bq(["show", "--format=none", table], cfg)
    if probe.returncode != 0:
        combined = (probe.stderr or "") + (probe.stdout or "")
        if "not found" in combined.lower():
            print(f"\nTable {fq_table} does not exist yet.\n"
                  f"The dataset owner creates it once - see "
                  f"docs/stamps-shipping-costs.md.", file=sys.stderr)
        elif is_permission_error(combined):
            permission_help(cfg, combined.strip())
        else:
            print(f"\nCould not inspect {fq_table}:\n{combined.strip()}", file=sys.stderr)
        return 1

    # A target still holding ="..."-escaped tracking numbers cannot be MERGEd
    # into: this loader now writes clean values, the MERGE joins on the raw
    # string, and every escaped label would INSERT a duplicate rather than
    # update. That is exactly what happened on 2026-09-03 -- 5,420 duplicate
    # rows and $61,913.11 of double-counted cost -- so refuse instead.
    probe = run_bq(["query", "--use_legacy_sql=false", "--format=csv",
                    f"SELECT COUNTIF(STARTS_WITH(tracking_number, '=')) AS n "
                    f"FROM `{fq_table}`"], cfg)
    if probe.returncode == 0:
        lines = [l for l in (probe.stdout or "").strip().splitlines() if l]
        escaped = lines[-1].strip() if len(lines) >= 2 else "0"
        if escaped.isdigit() and int(escaped) > 0:
            print(f"\nREFUSING TO LOAD: {escaped} rows in {fq_table} still carry\n"
                  f"spreadsheet-escaped tracking numbers (=\"94...\").\n\n"
                  f"This loader writes clean values and the MERGE matches on the raw\n"
                  f"string, so those rows would not update - each would gain a\n"
                  f"duplicate and double-count its cost.\n\n"
                  f"Repair the table first:\n"
                  f"  sql/stamps_repair_escaped_duplicates.sql  if duplicates already exist\n"
                  f"  sql/stamps_unescape_backfill.sql           if they do not\n",
                  file=sys.stderr)
            return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="stamps-load-"))
    ndjson = tmpdir / "rows.jsonl"
    run_ts = datetime.now(timezone.utc).isoformat()
    report = {}
    if cmd_prepare(paths, ndjson, run_ts, report) != 0:
        return 1

    # (c) Warning was not enough. When file ordering decides real money, a load
    #     needs an explicit decision rather than a message that scrolls past.
    delta = report.get("order_delta")
    if delta and not accept_order:
        print(f"\nREFUSING TO LOAD: file order changes the total by ${abs(delta):,.2f}.\n\n"
              f"The last occurrence of a tracking number wins, and a shell glob\n"
              f"orders files alphabetically rather than by export date - so this\n"
              f"load may be keeping stale amounts over post-audit ones.\n\n"
              f"Either load only the export that covers the whole period, or, if\n"
              f"the ordering above is genuinely what you want, re-run with\n"
              f"--accept-file-order.\n", file=sys.stderr)
        return 1

    # Weekly exports restate labels the previous export already covered, and
    # Stamps re-rates a label days after the fact. So this MERGEs on tracking
    # number rather than appending: a restated label updates in place, a
    # re-rated amount overwrites the stale one, and only genuinely new labels
    # insert. Appending would stack the same charge once per export that
    # mentions it, and nothing downstream would look broken - the totals would
    # simply be wrong.
    stage = f"{cfg['dataset']}.{cfg['table']}_stage"
    fq_stage = f"{cfg['project_id']}.{stage}"

    result = run_bq([
        "load", "--source_format=NEWLINE_DELIMITED_JSON", "--replace",
        stage, str(ndjson), str(SCHEMA_PATH),
    ], cfg)
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        if is_permission_error(combined):
            permission_help(cfg, combined.strip())
        else:
            print(f"\nStaging load failed (target table untouched):\n"
                  f"{combined.strip()}", file=sys.stderr)
        return 1
    print(f"staged {len(paths)} file(s) in {fq_stage}")

    # MERGE requires at most one source row per key, so the dedupe in `prepare`
    # is a precondition here, not a convenience. If it ever fails, BigQuery
    # rejects the whole statement rather than picking a row arbitrarily.
    updatable = ["ship_date", "carrier", "service", "weight_lb", "amount_paid",
                 "adjusted_amount", "order_id", "reference_1", "cost_code",
                 "to_name", "to_zip"]
    all_cols = ["ship_date", "tracking_number", "carrier", "service", "weight_lb",
                "amount_paid", "adjusted_amount", "order_id", "reference_1",
                "cost_code", "to_name", "to_zip", "source_file", "ingested_at",
                "ingested_by"]
    changed = "\n     OR ".join(f"T.{c} IS DISTINCT FROM S.{c}" for c in updatable)
    sets = ", ".join(f"{c} = S.{c}" for c in updatable + ["source_file", "ingested_at", "ingested_by"])
    cols = ", ".join(all_cols)
    vals = ", ".join(f"S.{c}" for c in all_cols)
    merge = (
        f"MERGE `{fq_table}` T USING `{fq_stage}` S\n"
        f"ON T.tracking_number = S.tracking_number\n"
        f"WHEN MATCHED AND ({changed}) THEN UPDATE SET {sets}\n"
        f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals})"
    )
    result = run_bq(["query", "--use_legacy_sql=false", merge], cfg)
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        if is_permission_error(combined):
            permission_help(cfg, combined.strip())
        else:
            print(f"\nMERGE failed (target table unchanged):\n{combined.strip()}",
                  file=sys.stderr)
        return 1
    print("merged into target")

    run_bq(["rm", "-f", "-t", stage], cfg)

    # The invariant that catches a double-count. One row per label, always.
    # Normalize before counting. Comparing raw strings let `="94..."` and
    # `94...` count as two distinct labels, so this check passed on a table
    # that was double-counting 5,420 rows. `rows` is reserved in BigQuery.
    check = (f"SELECT COUNT(*) AS row_count, "
             f"COUNT(DISTINCT REGEXP_REPLACE(tracking_number, r'^=\"(.*)\"$', r'\\1'))"
             f" AS label_count, "
             f"FORMAT('%.2f', SUM(amount_paid)) AS total FROM `{fq_table}`")
    result = run_bq(["query", "--use_legacy_sql=false", "--format=csv", check], cfg)
    if result.returncode == 0:
        lines = [l for l in (result.stdout or "").strip().splitlines() if l]
        if len(lines) >= 2:
            rows, labels, total = (lines[-1].split(",") + ["", "", ""])[:3]
            print(f"\ntable now: {rows} rows, {labels} distinct labels, ${total} total")
            if rows != labels:
                print("  INTEGRITY FAILURE: rows != distinct tracking numbers.\n"
                      "  Something is double-counting. Do not trust downstream "
                      "numbers until this is resolved.", file=sys.stderr)
                return 1
            print("  rows == distinct labels, no double-counting")

    print(f"\nLoaded into {fq_table}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("prepare", "load"):
        p = sub.add_parser(mode)
        p.add_argument("csv", nargs="+", type=Path)
        if mode == "prepare":
            p.add_argument("--out", type=Path, default=Path("stamps_rows.jsonl"))
        else:
            p.add_argument("--accept-file-order", action="store_true",
                           help="Proceed even though file ordering changes the "
                                "total. Only for a deliberate ordering.")
            p.add_argument("--no-impersonate", action="store_true",
                           help="Write with your own credentials instead of "
                                "impersonating the writer service account. Use "
                                "this when you can already write the dataset "
                                "directly.")
    args = parser.parse_args()

    for path in args.csv:
        if not path.exists():
            print(f"No such file: {path}", file=sys.stderr)
            return 1

    if args.mode == "prepare":
        return cmd_prepare(args.csv, args.out)

    cfg = load_config()
    if args.no_impersonate:
        cfg["impersonate_service_account"] = ""
    return cmd_load(args.csv, cfg, args.accept_file_order)


if __name__ == "__main__":
    sys.exit(main())

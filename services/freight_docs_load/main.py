#!/usr/bin/env python3
"""Cloud Run job: load processed freight documents into BigQuery.

Runs on Cloud Scheduler (hourly). Two steps, in order:

1. Landing table. Every processed/**/<sha256>.ndjson whose sha256 is not yet
   in `finance.freight_documents_raw` (all of them with --full) is staged in
   `finance.freight_documents_raw_stage` (bq load --replace), MERGEd into the
   landing table on sha256, and the stage is dropped. A matched row is only
   overwritten by a newer extraction (extracted_at). Post-load invariant: one
   row per sha256 in the table, and every staged sha256 present - else exit 6.

2. CBP 7501s. Every landing row with doc_type customs_7501, parse_status
   parsed, whose sha256 is not yet a `finance.customs_entries.source_sha256`
   and has no quarantine/<sha256>.json, is loaded by running
   tools/customs_entries_load.py on the PDF, one file per call, exactly as an
   operator would. Its rules apply unchanged: same entry + same SHA-256 is
   skipped, a different PDF for a loaded entry is refused, the form's
   arithmetic must hold, and it verifies its own write (exit 6). --replace and
   --allow-mismatch are never passed. A refusal (exit 2 or 3) writes
   quarantine/<sha256>.json with the loader's output, so the document is
   never retried; a person decides what to do with it.

Writes as the job's service account (freight-docs-load@). Needs gcloud, bq and
pdftotext in the image. --dry-run reads and reports, writes nothing.

Exit codes: 0 ok · 2 bad usage · 5 bq/gcloud error · 6 post-load verification failed
            (landing invariant, or the 7501 loader's own verify) · 7 finance.customs_entries
            does not exist yet (landing step still ran)
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = Path(os.environ.get("FREIGHT_DOCS_TOOLS_DIR") or ROOT / "tools")
sys.path.insert(0, str(TOOLS_DIR))
import customs_entries_load as cel  # noqa: E402  (gcloud_account; the CLI itself does the 7501 loads)

LOADER = TOOLS_DIR / "customs_entries_load.py"
SCHEMA_PATH = Path(os.environ.get("FREIGHT_DOCS_SCHEMA") or ROOT / "schemas" / "freight_documents_raw.json")
PROJECT = os.environ.get("FREIGHT_DOCS_PROJECT", "americanflat")
BUCKET = os.environ.get("FREIGHT_DOCS_BUCKET", "americanflat-freight-docs")
DATASET = os.environ.get("FREIGHT_DOCS_DATASET", "finance")
TABLE = os.environ.get("FREIGHT_DOCS_RAW_TABLE", "freight_documents_raw")
STAGE = f"{TABLE}_stage"
CUSTOMS_TABLE = "customs_entries"  # owned by tools/customs_entries_load.py
LOCATION = "US"
SHA_RE = re.compile(r"/([0-9a-f]{64})\.(?:ndjson|json)$")
JSON_COLS = {"tables", "entities", "typed", "meta"}


class Fail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def log(msg, severity="INFO"):
    print(json.dumps({"severity": severity, "message": msg}), flush=True)


# --------------------------------------------------------------------------- CLIs

def run(cmd, data=None):
    return subprocess.run(cmd, input=data, capture_output=True)


def bq(args, check=True):
    r = run([shutil.which("bq") or "bq", f"--project_id={PROJECT}", f"--location={LOCATION}", "--quiet"] + args)
    if check and r.returncode != 0:
        raise Fail(5, f"bq {' '.join(args[:2])} failed: {(r.stderr or r.stdout).decode(errors='replace').strip()}")
    return r


def bq_query(sql):
    r = bq(["query", "--use_legacy_sql=false", "--format=json", "--max_rows=100000", sql])
    out = r.stdout.decode().strip()
    return json.loads(out) if out.startswith("[") else []


def gcloud(args, data=None, check=True):
    r = run([shutil.which("gcloud") or "gcloud", "storage"] + args, data)
    if check and r.returncode != 0:
        raise Fail(5, f"gcloud storage {args[0]} failed: {r.stderr.decode(errors='replace').strip()}")
    return r


def gcs_ls(pattern):
    r = gcloud(["ls", pattern], check=False)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace")
        if "matched no objects" in err or "One or more URLs matched no objects" in err:
            return []
        raise Fail(5, f"gcloud storage ls {pattern} failed: {err.strip()}")
    return [l.strip() for l in r.stdout.decode().splitlines() if l.strip().startswith("gs://")]


def gcs_create(uri, data):
    """Create unless it exists (ifGenerationMatch=0). True = written."""
    r = gcloud(["cp", "--if-generation-match=0", "-", uri], data=data, check=False)
    if r.returncode == 0:
        return True
    if gcloud(["objects", "describe", uri], check=False).returncode == 0:
        return False
    raise Fail(5, f"write {uri} failed: {r.stderr.decode(errors='replace').strip()}")


def quarantine(sha, reason, gcs_uri, dry_run):
    body = {"sha256": sha, "stage": "load", "reason": reason, "gcs_uri": gcs_uri,
            "at": datetime.now(timezone.utc).isoformat(), "by": "freight-docs-load"}
    uri = f"gs://{BUCKET}/quarantine/{sha}.json"
    if dry_run:
        log(f"dry run: would quarantine {sha}: {reason[:200]}", "WARNING")
        return
    gcs_create(uri, json.dumps(body, indent=2).encode())
    log(f"quarantined {sha}: {reason[:500]}", "WARNING")


# --------------------------------------------------------------------------- step 1: landing table

def stage_row(rec, run_ts, who):
    """Every stage column is STRING; the MERGE casts. JSON columns carry JSON text."""
    row = {}
    for f in json.loads(SCHEMA_PATH.read_text()):
        v = rec.get(f["name"])
        if f["name"] in JSON_COLS:
            row[f["name"]] = None if v is None else json.dumps(v, ensure_ascii=False)
        else:
            row[f["name"]] = None if v is None else str(v)
    row["ingested_at"], row["ingested_by"] = run_ts, who
    return row


def merge_sql(fields):
    fq, fq_stage = f"`{PROJECT}.{DATASET}.{TABLE}`", f"`{PROJECT}.{DATASET}.{STAGE}`"

    def cast(f):
        n, t = f["name"], f["type"]
        if t == "JSON":
            return f"PARSE_JSON({n}, wide_number_mode => 'round') AS {n}"
        if t == "TIMESTAMP":
            return f"TIMESTAMP({n}) AS {n}"
        if t == "INT64":
            return f"CAST({n} AS INT64) AS {n}"
        return n

    names = [f["name"] for f in fields]
    select = ",\n       ".join(cast(f) for f in fields)
    sets = ", ".join(f"{n} = S.{n}" for n in names if n != "sha256")
    return (f"MERGE {fq} T\n"
            f"USING (SELECT {select}\n       FROM {fq_stage}) S\n"
            f"ON T.sha256 = S.sha256\n"
            f"WHEN MATCHED AND S.extracted_at > T.extracted_at THEN UPDATE SET {sets}\n"
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}) "
            f"VALUES ({', '.join('S.' + n for n in names)})")


def load_landing(full, dry_run):
    fq = f"{PROJECT}.{DATASET}.{TABLE}"
    files = {}
    for uri in gcs_ls(f"gs://{BUCKET}/processed/**"):
        m = SHA_RE.search(uri)
        if m and uri.endswith(".ndjson"):
            files.setdefault(m.group(1), []).append(uri)
    known = set() if full else {r["sha256"] for r in bq_query(f"SELECT sha256 FROM `{fq}`")}
    todo = {sha: uris for sha, uris in files.items() if sha not in known}
    log(f"landing: {len(files)} processed document(s), {len(todo)} to load")
    if not todo:
        return 0

    run_ts = datetime.now(timezone.utc).isoformat()
    who = cel.gcloud_account()
    rows = []
    for sha, uris in sorted(todo.items()):
        recs = [json.loads(gcloud(["cat", u]).stdout.decode("utf-8").strip().splitlines()[0]) for u in uris]
        # The same bytes routed to two doc_types (two searches matched) give two
        # NDJSONs with one sha256. One row per document: prefer the typed 7501,
        # then the newest extraction.
        recs.sort(key=lambda r: (r.get("parse_status") == "parsed", r.get("extracted_at") or ""), reverse=True)
        if len(recs) > 1:
            log(f"{sha}: {len(recs)} processed files, keeping {recs[0]['processed_uri']}", "WARNING")
        rows.append(stage_row(recs[0], run_ts, who))
    if dry_run:
        for r in rows:
            log(f"dry run: would load {r['sha256']} {r['doc_type']} {r['parse_status']}")
        return len(rows)

    fields = json.loads(SCHEMA_PATH.read_text())
    with tempfile.TemporaryDirectory(prefix="freight-docs-load-") as tmp:
        ndjson, schema = Path(tmp) / "rows.ndjson", Path(tmp) / "stage_schema.json"
        ndjson.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        schema.write_text(json.dumps([{"name": f["name"], "type": "STRING"} for f in fields]))
        bq(["load", "--source_format=NEWLINE_DELIMITED_JSON", "--replace",
            f"{DATASET}.{STAGE}", str(ndjson), str(schema)])
    try:
        bq(["query", "--use_legacy_sql=false", merge_sql(fields)])
    finally:
        bq(["rm", "-f", "-t", f"{DATASET}.{STAGE}"], check=False)

    # Invariant: one row per document, and everything staged landed.
    shas = ", ".join(f"'{r['sha256']}'" for r in rows)
    got = bq_query(f"SELECT COUNT(*) AS n, COUNT(DISTINCT sha256) AS d, "
                   f"COUNTIF(sha256 IN ({shas})) AS staged FROM `{fq}`")[0]
    if got["n"] != got["d"] or int(got["staged"]) != len(rows):
        raise Fail(6, f"landing VERIFY FAILED: {got['n']} rows, {got['d']} distinct sha256, "
                      f"{got['staged']} of {len(rows)} staged documents present")
    log(f"landing: merged {len(rows)}; table has {got['n']} rows = {got['d']} distinct sha256")
    return len(rows)


# --------------------------------------------------------------------------- step 2: 7501s

def safe_name(name, sha):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").name).strip("._")
    return name if name.lower().endswith(".pdf") else f"{sha}.pdf"


def load_7501s(dry_run):
    if bq(["show", "--format=none", f"{DATASET}.{CUSTOMS_TABLE}"], check=False).returncode != 0:
        raise Fail(7, f"{DATASET}.{CUSTOMS_TABLE} does not exist: an operator must run "
                      f"`python3 tools/customs_entries_load.py --init` first. 7501s not loaded.")
    quarantined = {m.group(1) for u in gcs_ls(f"gs://{BUCKET}/quarantine/*.json") if (m := SHA_RE.search(u))}
    todo = [r for r in bq_query(
        f"SELECT sha256, gcs_uri, source_file FROM `{PROJECT}.{DATASET}.{TABLE}` "
        f"WHERE doc_type = 'customs_7501' AND parse_status = 'parsed' "
        f"AND sha256 NOT IN (SELECT source_sha256 FROM `{PROJECT}.{DATASET}.{CUSTOMS_TABLE}` "
        f"WHERE source_sha256 IS NOT NULL) "
        f"ORDER BY email_date, sha256") if r["sha256"] not in quarantined]
    log(f"7501: {len(todo)} to load ({len(quarantined)} document(s) in quarantine)")
    loaded = 0
    for r in todo:
        sha = r["sha256"]
        with tempfile.TemporaryDirectory(prefix="freight-7501-") as tmp:
            pdf = Path(tmp) / safe_name(r.get("source_file"), sha)
            pdf.write_bytes(gcloud(["cat", r["gcs_uri"]]).stdout)
            cmd = [sys.executable, str(LOADER)] + (["--dry-run"] if dry_run else []) + [str(pdf)]
            p = subprocess.run(cmd, capture_output=True, text=True)
        out = (p.stdout + p.stderr).strip()
        print(out, flush=True)
        if p.returncode == 0:
            loaded += 1
        elif p.returncode in (2, 3):
            # Refused: unreadable, form arithmetic fails, or a different PDF for
            # an entry already loaded (a correction). Never forced, never retried.
            quarantine(sha, f"customs_entries_load.py exit {p.returncode}: {out[-1500:]}", r["gcs_uri"], dry_run)
        elif p.returncode == 6:
            raise Fail(6, f"customs_entries_load.py verify failed for {sha} - check {CUSTOMS_TABLE} by hand")
        else:
            raise Fail(5, f"customs_entries_load.py exit {p.returncode} for {sha}; retried next run")
    log(f"7501: {loaded} loaded or already present")
    return loaded


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true",
                    help="restage every processed file, not just new sha256s (re-extractions update rows)")
    ap.add_argument("--dry-run", action="store_true", help="report what would load; write nothing")
    a = ap.parse_args()
    try:
        load_landing(a.full, a.dry_run)
        load_7501s(a.dry_run)
    except Fail as f:
        log(str(f), "ERROR")
        return f.code
    return 0


if __name__ == "__main__":
    sys.exit(main())

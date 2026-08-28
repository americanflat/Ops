#!/usr/bin/env python3
"""Run the Yusen Invoices dashboard refresh with BigQuery-backed state.

Background
----------
The refresh itself lives in `refresh_artifact_dashboard.py` in
anthony-amf/americanflat-ops-director. That script gates the republish on a
fingerprint of the rendered data, and it records the last published fingerprint
in a local `.dashboard-state.json` which it expects the caller to commit and
push back to the repo.

That push never worked from the scheduled sessions — they only have anonymous
read on that repo — so the state was permanently stuck at its 2026-08-07 value,
the gate always saw a stale fingerprint, and every run republished identical
data. See docs/yusen-dashboard-refresh.md.

This wrapper moves the state somewhere the scheduled sessions *can* write:
`americanflat.observability.pipeline_runs`, reached over the same
proxy-injected BigQuery credentials the refresh already uses. No git push, no
new credentials, and no DDL — that table already exists, and its `extra` JSON
column carries the fingerprint. (A dedicated `finance.dashboard_state` table
was the first choice but `bigquery.tables.create` is denied on that dataset.)

The upstream script is not modified. It takes `--state <path>`; we hand it a
file seeded from BigQuery on the way in, and publish what it wrote back to
BigQuery on the way out.

Fail-open
---------
Every BigQuery interaction here degrades to "republish anyway":

  * state read fails or finds nothing -> no seed file -> upstream sees no
    previous fingerprint -> CHANGED -> the dashboard is republished.
  * state write fails -> warn, exit 0 -> the next run sees a stale fingerprint
    and republishes.

The failure mode is a redundant republish, never a stale dashboard. That is the
same behaviour the pipeline had before this wrapper existed, so a permissions
problem here can only cost efficiency, not correctness.

Usage
-----
    python3 yusen_dashboard_refresh.py run
        Clone the refresh repo, seed state from BigQuery, run the refresh.
        Prints the upstream's contract line last:
            NO_CHANGE <fp>          nothing to do
            CHANGED <path> <fp>     publish <path> to the artifact

    python3 yusen_dashboard_refresh.py record
        Record the just-published fingerprint in BigQuery. Run this only
        after the Artifact publish actually succeeded.
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = "americanflat"
STATE_TABLE = "americanflat.observability.pipeline_runs"
DASHBOARD = "yusen-invoices"

SOURCE_REPO = "https://github.com/anthony-amf/americanflat-ops-director"
SOURCE_BRANCH = "claude/website-auto-refresh-efficiency-9x474j"

WORK = Path("/tmp/yusen-refresh")
DIRECTOR = WORK / "director"
STATE = WORK / "state.json"
OUT = WORK / "yusen_invoices_artifact.html"


def bq(sql: str) -> dict:
    """Run one query against BigQuery over the proxy-injected credentials."""
    req = urllib.request.Request(
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
        data=json.dumps({"query": sql, "useLegacySql": False,
                         "timeoutMs": 60000}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read())
    if "error" in out:
        raise RuntimeError(out["error"].get("message", "unknown BigQuery error"))
    return out


def sql_str(value: str) -> str:
    """Quote a Python string as a BigQuery string literal."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def read_state() -> dict | None:
    """Most recent published state for this dashboard, or None."""
    rows = bq(f"""
        SELECT JSON_VALUE(extra, '$.fingerprint') AS fingerprint,
               rows_written
        FROM `{STATE_TABLE}`
        WHERE JSON_VALUE(extra, '$.dashboard') = {sql_str(DASHBOARD)}
          AND status = 'published'
        ORDER BY started_at DESC
        LIMIT 1
    """).get("rows", [])
    if not rows:
        return None
    cells = rows[0]["f"]
    fingerprint = cells[0].get("v")
    if not fingerprint:
        return None
    written = cells[1].get("v")
    return {"fingerprint": fingerprint,
            "rows": int(written) if written is not None else 0}


def write_state(state: dict) -> None:
    """Append this publish to the pipeline-run log."""
    fingerprint = state["fingerprint"]
    extra = json.dumps({
        "dashboard": DASHBOARD,
        "fingerprint": fingerprint,
        "paid": state.get("paid", 0),
        "artifact_url": state.get("artifact_url", ""),
    })
    bq(f"""
        INSERT INTO `{STATE_TABLE}`
          (run_id, repo, started_at, ended_at, status, rows_written, extra)
        VALUES (
          CONCAT('yusen-dashboard-refresh-',
                 FORMAT_TIMESTAMP('%Y%m%dT%H%M%SZ', CURRENT_TIMESTAMP())),
          'anthony-amf/americanflat-ops-director',
          CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), 'published',
          {int(state.get("rows", 0))},
          PARSE_JSON({sql_str(extra)})
        )
    """)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_run() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    clone = subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "-b", SOURCE_BRANCH,
         SOURCE_REPO, str(DIRECTOR)],
        capture_output=True, text=True,
    )
    if clone.returncode != 0:
        sys.stderr.write(clone.stderr)
        return clone.returncode

    # Seed the upstream script's state file from BigQuery. On any failure we
    # leave it absent, which makes the gate open and forces a republish.
    try:
        previous = read_state()
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, KeyError) as exc:
        print(f"  state read failed ({exc}); treating as no previous publish")
        previous = None
    if previous:
        STATE.write_text(json.dumps(previous) + "\n")
        print(f"  last published fingerprint {previous['fingerprint']} "
              f"({previous['rows']} rows) from BigQuery")
    else:
        print("  no previous publish on record; the refresh will republish")

    refresh = subprocess.run(
        [sys.executable, "refresh_artifact_dashboard.py",
         "--state", str(STATE), "--out", str(OUT)],
        cwd=DIRECTOR, capture_output=True, text=True,
    )
    sys.stderr.write(refresh.stderr)
    sys.stdout.write(refresh.stdout)
    return refresh.returncode


def cmd_record() -> int:
    try:
        state = json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"  no state file to record ({exc}) — nothing written")
        return 0
    try:
        write_state(state)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, KeyError) as exc:
        # Never fail the run over this. The publish already happened; the only
        # cost is that the next run cannot skip and will republish.
        print(f"  WARNING: could not record state in BigQuery ({exc})")
        print("  the dashboard is published; the next run will republish")
        return 0
    print(f"  recorded fingerprint {state['fingerprint']} "
          f"({state.get('rows', 0)} rows) in {STATE_TABLE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "record"])
    args = ap.parse_args()
    return cmd_run() if args.command == "run" else cmd_record()


if __name__ == "__main__":
    sys.exit(main())

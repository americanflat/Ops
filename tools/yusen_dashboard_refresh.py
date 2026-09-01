#!/usr/bin/env python3
"""Run the Yusen Invoices dashboard refresh, gated on the live artifact.

Background
----------
The refresh itself lives in `refresh_artifact_dashboard.py` in
anthony-amf/americanflat-ops-director. It renders the artifact HTML from
`americanflat.finance.yusen_invoices` and gates the republish on a fingerprint
of the rendered rows, comparing against a fingerprint the *caller* has to store
and hand back via `--state`.

Storing that fingerprint is the part that has never worked:

  * `.dashboard-state.json` in the upstream repo — the scheduled sessions only
    have anonymous read there, so the push never landed and the state froze at
    its 2026-08-07 value.
  * `americanflat.observability.pipeline_runs` — the warehouse credential these
    sessions use cannot write it (HTTP 403, still denied as of 2026-09-01), and
    `bigquery.tables.create` is denied on `finance`, so there was nowhere else
    to put it. That table is empty; nothing has ever been recorded in it.

This wrapper drops the external store entirely. **The live artifact is the
state.** The rendered page carries every row it displays in a `const DATA`
literal, so the fingerprint of what is currently published can be recovered
from the published page itself — which the calling session already has to read
before it may publish (an unread artifact refuses the publish as a conflict).
No credentials, no table, no git push, and nothing to fall out of sync.

Churn normalization
-------------------
A raw fingerprint of the rows can never be stable, which is the second reason
the gate never closed. The nightly validation Routine re-stamps
`validation_report` with a fresh `[AUTO <today>]` block and bumps `validated_at`
to today on ~76 invoices every night, whether or not any finding changed. On
2026-09-01 that accounted for **all 76** differences between the live page and
a fresh render — every one of them a date string, with no change to a status,
an amount, a verdict or a document link.

So the fingerprint here normalizes that churn away: `validated_at` is dropped,
and a `[TAG YYYY-MM-DD]` stamp inside a report is reduced to `[TAG]`. A genuinely
new block still changes the text beyond its date, and a real change to a status,
amount, payment or link still changes the row, so both still republish.

Usage
-----
    python3 yusen_dashboard_refresh.py run --published <read-file>
        Clone the refresh repo, render, and compare against the live page.
        <read-file> is the local file the Artifact tool's `read` action saves.
        Prints the contract line last:
            NO_CHANGE <fp>          the live page already shows this data
            CHANGED <path> <fp>     publish <path> to the artifact

        Omit --published (or hand it something unreadable) and the run
        fails open with CHANGED — a redundant republish, never a stale page.

    python3 yusen_dashboard_refresh.py verify --published <read-file>
        Independent proof the publish landed: re-derive the fingerprint of the
        live page and compare it to what `run` rendered. Prints VERIFIED <fp>,
        or MISMATCH and exits 1. Pass a *fresh* read taken after the publish.

Fail-open
---------
Anything this wrapper cannot determine resolves to "republish anyway". The
failure mode is a redundant republish, never a stale dashboard.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SOURCE_REPO = "https://github.com/anthony-amf/americanflat-ops-director"
SOURCE_BRANCH = "claude/website-auto-refresh-efficiency-9x474j"

WORK = Path("/tmp/yusen-refresh")
DIRECTOR = WORK / "director"
OUT = WORK / "yusen_invoices_artifact.html"
RENDERED_FP = WORK / "rendered.fp"

DATA_MARKER = "const DATA = "

# `[AUTO 2026-09-01]`, `[STEDI 2026-08-31]`, `[VAS SWEEP 2026-08-05]` — the date
# a check ran is not a finding, and the validator rewrites it nightly.
STAMP_DATE = re.compile(r"\[([A-Z][A-Z /]*) \d{4}-\d{2}-\d{2}\]")


# --------------------------------------------------------------------------
# fingerprinting
# --------------------------------------------------------------------------

def upstream_fingerprint():
    """The upstream's own hash function, imported rather than reimplemented.

    Keeps this wrapper's fingerprints and the upstream's identical in method,
    so the two can never drift apart. Import is safe: that module does its work
    under `if __name__ == "__main__"`.
    """
    sys.path.insert(0, str(DIRECTOR))
    import refresh_artifact_dashboard as upstream
    return upstream.fingerprint


def rows_of_page(path: Path) -> list:
    """The rows a rendered dashboard page displays, from its `const DATA`."""
    text = path.read_text(errors="replace")
    at = text.find(DATA_MARKER)
    if at < 0:
        raise ValueError(f"no {DATA_MARKER!r} literal in {path}")
    rows, _ = json.JSONDecoder().raw_decode(text, at + len(DATA_MARKER))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{DATA_MARKER!r} in {path} is not a non-empty list")
    return rows


def normalize(rows: list) -> list:
    """Strip the nightly validator's date churn; keep every real field."""
    out = []
    for row in rows:
        row = dict(row)
        row.pop("validated_at", None)
        report = row.get("validation_report")
        if isinstance(report, str):
            row["validation_report"] = STAMP_DATE.sub(r"[\1]", report)
        out.append(row)
    return out


def page_fingerprint(path: Path) -> str:
    return upstream_fingerprint()(normalize(rows_of_page(path)))


def live_fingerprint(published: str | None) -> tuple[str | None, str]:
    """Fingerprint of the currently-published page, plus why if unavailable."""
    if not published:
        return None, "no --published file given"
    path = Path(published)
    if not path.is_file():
        return None, f"{path} does not exist"
    try:
        return page_fingerprint(path), ""
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, str(exc)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def clone_source(attempts: int = 3) -> int:
    """Clone the refresh repo, retrying a failed clone."""
    delay = 5
    for attempt in range(1, attempts + 1):
        if DIRECTOR.exists():
            shutil.rmtree(DIRECTOR)
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "-b", SOURCE_BRANCH,
             SOURCE_REPO, str(DIRECTOR)],
            capture_output=True, text=True,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if clone.returncode == 0:
            if attempt > 1:
                print(f"  clone succeeded on attempt {attempt}")
            return 0
        print(f"  clone attempt {attempt}/{attempts} failed "
              f"(rc={clone.returncode}): {clone.stderr.strip()[:200]}")
        if attempt < attempts:
            time.sleep(delay)
            delay *= 3
    sys.stderr.write(f"could not clone {SOURCE_REPO} after {attempts} attempts\n")
    return 1


def cmd_run(published: str | None) -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    rc = clone_source()
    if rc != 0:
        return rc

    # Render unconditionally. The upstream's own gate needs a state file this
    # environment cannot keep, so --force renders every time and the comparison
    # below decides. --state points at a throwaway path so the upstream never
    # writes into its own checkout.
    refresh = subprocess.run(
        [sys.executable, "refresh_artifact_dashboard.py", "--force",
         "--state", str(WORK / "upstream-state.json"), "--out", str(OUT)],
        cwd=DIRECTOR, capture_output=True, text=True,
    )
    sys.stderr.write(refresh.stderr)
    # Drop the upstream's own verdict and fingerprint lines: they would be a
    # second, conflicting answer on the last line of stdout, and its raw
    # fingerprint is not the churn-normalized one this wrapper gates on.
    for line in refresh.stdout.splitlines():
        if not line.startswith(("CHANGED ", "NO_CHANGE ", "  fingerprint ")):
            print(line)
    if refresh.returncode != 0:
        sys.stderr.write(f"refresh failed (rc={refresh.returncode})\n")
        return refresh.returncode
    if not OUT.is_file():
        sys.stderr.write(f"refresh reported success but {OUT} is missing\n")
        return 1

    try:
        rendered = page_fingerprint(OUT)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # A page we cannot fingerprint is a broken render, not a gate question.
        sys.stderr.write(f"cannot fingerprint the rendered page: {exc}\n")
        return 1
    RENDERED_FP.write_text(rendered + "\n")

    live, why = live_fingerprint(published)
    if live is None:
        print(f"  live fingerprint unavailable ({why}); republishing to be safe")
        print(f"CHANGED {OUT} {rendered}")
        return 0

    print(f"  live page {live} · rendered {rendered}")
    if live == rendered:
        print(f"NO_CHANGE {rendered}")
    else:
        print(f"CHANGED {OUT} {rendered}")
    return 0


def cmd_verify(published: str | None, expect: str | None) -> int:
    if expect is None:
        try:
            expect = RENDERED_FP.read_text().strip()
        except OSError as exc:
            sys.stderr.write(f"no rendered fingerprint to verify against ({exc}); "
                             f"run `run` first or pass --expect\n")
            return 1
    live, why = live_fingerprint(published)
    if live is None:
        sys.stderr.write(f"MISMATCH could not read the live page ({why})\n")
        return 1
    if live != expect:
        sys.stderr.write(f"MISMATCH live={live} rendered={expect}\n")
        sys.stderr.write("the publish did not land — the artifact still shows "
                         "older data\n")
        return 1
    print(f"VERIFIED {live}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "verify"])
    ap.add_argument("--published", metavar="PATH",
                    help="local file saved by the Artifact tool's read action")
    ap.add_argument("--expect", metavar="FP",
                    help="verify against this fingerprint instead of the last render")
    args = ap.parse_args()
    if args.command == "run":
        return cmd_run(args.published)
    return cmd_verify(args.published, args.expect)


if __name__ == "__main__":
    sys.exit(main())

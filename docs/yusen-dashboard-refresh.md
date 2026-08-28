# Yusen Invoices dashboard — refresh pipeline

The finance-facing invoice dashboard is a Claude Artifact:
<https://claude.ai/code/artifact/23dd148b-1fb0-4219-80e1-53ca8d9d3d97>

It is a **static page with the data baked in**. It makes no live query at view
time, so it only changes when something republishes it.

## How it refreshes

Two scheduled Routines republish it on weekdays:

| Routine | Cron (UTC) |
| --- | --- |
| `refresh-yusen-artifact-830am-330pm` | `30 12,19 * * 1-5` |
| `refresh-yusen-artifact-noon-6pm` | `0 16,22 * * 1-5` |

Each one clones `anthony-amf/americanflat-ops-director`
(branch `claude/website-auto-refresh-efficiency-9x474j`) and runs
`refresh_artifact_dashboard.py`. That script selects every row of
`americanflat.finance.yusen_invoices`, fingerprints the rendered fields, and
prints either `NO_CHANGE <fp>` or `CHANGED <path> <fp>`. On `CHANGED` the
session publishes the rendered HTML back to the artifact URL above.

Passing `url:` to the Artifact tool is mandatory. Without it the run mints a
duplicate artifact and the real dashboard goes stale silently.

## Outage 2026-08-21 → 2026-08-28

The dashboard froze with data through 2026-08-20 while BigQuery went on to
2026-08-26 — four invoices missing (757556, 757557, 757680, 757696).

**Cause.** Both Routines began with `cd /home/user/americanflat-ops-director`.
Environment `env_01VYuZvJjeBFovELdTWzcubw` is sourced from `americanflat/Ops`,
which clones to `/home/user/Ops`, so that directory does not exist. The `&&`
chain short-circuited on the failed `cd` and the refresh script never ran.

**Why it looked healthy.** The Routines kept reporting
`ROUTINE_RUN_STATUS_SUCCEEDED`. That status only means the session finished
cleanly — and the prompt told it to report the error and stop, which it did.
A green Routine is not evidence the dashboard was republished.

**Fix.** Step 1 of both prompts now clones a fresh checkout instead of assuming
a pre-existing directory, so the job no longer depends on how the environment's
sources are configured.

**Second fault — the publish itself.** Fixing the path was not enough. The next
two runs completed, reported SUCCEEDED, and still produced no new artifact
version. The cause: a publish from a session that has never *read* the artifact
is refused as a conflict, and these sessions only ever published. Both prompts
now call the Artifact tool with `action: "read"` before publishing. Verified
2026-08-28 16:28:35Z — a fired run published for the first time since Aug 21.

Neither fault announced itself. Both runs reported `ROUTINE_RUN_STATUS_SUCCEEDED`
because the session finished cleanly; the prompt told it to report the error and
stop, which it did. **Never treat a green Routine as evidence the dashboard was
republished — check the artifact's version timestamp.**

## Which branch the Routines clone

Step 1 of both Routines clones `main`, verifies `tools/yusen_dashboard_refresh.py`
actually arrived, and falls back to `claude/invoice-dashboard-missing-data-axicr7`
if it did not. It echoes a `SOURCE:` line naming the branch and commit it used.

The fallback is not decoration. After `main` was fast-forwarded to carry this
tool (2026-08-28), two Routine runs cloning `main` failed to publish, while runs
cloning the pre-merge branch published fine — the branch was the only variable.
The leading theory is that those sessions' git proxy served a stale `main`
(still the README-only initial commit), so the `python3 .../tools/...` call died
immediately; that matches the ~60s, ~2k-output-token signature of the failures.
It was never confirmed, because a Routine session's stdout is not readable from
another session.

Consequences:

* **Do not delete `claude/invoice-dashboard-missing-data-axicr7`** until a
  `SOURCE:` line in a run's reply reads `main`. It is a live fallback.
* Keep that branch pointing at the same commit as `main`. Push to both.
* Once `SOURCE:` reports `main`, the fallback is inert and can be removed
  along with the branch.

## Where the fingerprint lives

`refresh_artifact_dashboard.py` gates the republish on a fingerprint of the
rendered data, and records the last published fingerprint via `--state <path>`.
It originally wrote that to `.dashboard-state.json` in its own repo and expected
the caller to commit and push it back.

That push never worked. The scheduled sessions only have anonymous read on
`anthony-amf/americanflat-ops-director`, so the state froze at its 2026-08-07
value, the gate always saw a stale fingerprint, and every run republished
identical data — defeating the whole point of the gate.

State now lives in BigQuery instead, in
`americanflat.observability.pipeline_runs`, reached over the same
proxy-injected credentials the refresh already uses. One row per publish, with
the fingerprint in the existing `extra` JSON column. No git push, no new
credentials, and no DDL. (A dedicated `finance.dashboard_state` table was the
first choice, but `bigquery.tables.create` is denied on that dataset.)

`tools/yusen_dashboard_refresh.py` in this repo is the wrapper that does it:

* `run` — clone the refresh repo, seed the upstream state file from BigQuery,
  run the refresh, and print the upstream's `NO_CHANGE` / `CHANGED` line.
* `record` — append the just-published fingerprint to `pipeline_runs`. Run it
  only after the Artifact publish actually succeeded.

The upstream script is untouched; the wrapper only swaps the state backend via
its existing `--state` flag. Nothing is duplicated between the two repos.

### Fail-open

Every BigQuery interaction in the wrapper degrades to "republish anyway":

* state read fails or finds nothing → no seed file → the gate opens → the
  dashboard is republished.
* state write fails → warning, exit 0 → the next run sees a stale fingerprint
  and republishes.

So a permissions problem costs a redundant republish, never a stale dashboard —
the same behaviour the pipeline had before the wrapper existed.

### Not yet working: the write is denied

As of 2026-08-28 the state write does **not** land. The warehouse credential
these sessions use can read everything, and can run DML against `finance`, but:

| Operation | Result |
| --- | --- |
| `bigquery.tables.updateData` on `observability.pipeline_runs` | DENIED |
| `bigquery.tables.create` on `finance` | DENIED |
| DML on existing `finance` tables (e.g. `yusen_invoices`) | allowed |

`finance` holds only `freight_invoices`, `vendor_payment_invoices`,
`vendor_payments` and `yusen_invoices` — none of them a sane home for dashboard
state — so there is currently nowhere the state can be written.

The gate therefore still fails open on every run: `record` prints a WARNING,
exits 0, and the next run republishes. That is the pre-existing behaviour, so
nothing regressed, but the redundant republishes continue.

Smallest fix: grant `bigquery.tables.updateData` (or `roles/bigquery.dataEditor`)
on the `observability` dataset. No code change — the wrapper starts working the
moment the grant lands. Alternative: grant `bigquery.tables.create` on `finance`
and give the state its own table there.

To inspect the recorded state:

```sql
SELECT started_at, rows_written, JSON_VALUE(extra, '$.fingerprint') AS fingerprint
FROM `americanflat.observability.pipeline_runs`
WHERE JSON_VALUE(extra, '$.dashboard') = 'yusen-invoices'
ORDER BY started_at DESC
LIMIT 10
```

## Checking it by hand

```
git clone --quiet --depth 1 -b claude/invoice-dashboard-missing-data-axicr7 \
  https://github.com/americanflat/Ops /tmp/ops && \
  python3 /tmp/ops/tools/yusen_dashboard_refresh.py run
```

Compare against BigQuery directly:

```sql
SELECT MAX(date) AS max_date, COUNT(*) AS n
FROM `americanflat.finance.yusen_invoices`
```

If the dashboard's newest invoice trails `max_date`, the publish step is broken —
not the ingestion.

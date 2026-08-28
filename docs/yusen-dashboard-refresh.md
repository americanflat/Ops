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

**Fix.** Step 1 of both prompts now clones a fresh checkout into
`/tmp/yusen-refresh` instead of assuming a pre-existing directory, so the job
no longer depends on how the environment's sources are configured.

## Known gap: the fingerprint gate does not persist

`.dashboard-state.json` records the last published fingerprint so an unchanged
day can skip the republish. It has not been committed since 2026-08-07 — the
Routine's push step needs write access to `anthony-amf/americanflat-ops-director`,
and the refresh sessions only have anonymous read.

So the gate always sees a stale fingerprint, always reports `CHANGED`, and
republishes every run. That is wasteful but harmless, and it is the behaviour
that kept the dashboard current before the path broke. Step 3 of each prompt
now treats the push as best-effort and does not fail the run over it.

To actually close the gap, the refresh sessions need push credentials for that
repo, or the state file needs to live somewhere they can write.

## Checking it by hand

```
rm -rf /tmp/yusen-refresh \
  && GIT_LFS_SKIP_SMUDGE=1 git clone --quiet --depth 1 \
       -b claude/website-auto-refresh-efficiency-9x474j \
       https://github.com/anthony-amf/americanflat-ops-director /tmp/yusen-refresh \
  && cd /tmp/yusen-refresh && python3 refresh_artifact_dashboard.py --check-only
```

Compare against BigQuery directly:

```
SELECT MAX(date) AS max_date, COUNT(*) AS n
FROM `americanflat.finance.yusen_invoices`
```

If the dashboard's newest invoice trails `max_date`, the publish step is broken —
not the ingestion.

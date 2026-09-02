# Yusen Invoices dashboard — refresh pipeline

The finance-facing invoice dashboard is a Claude Artifact:
<https://claude.ai/code/artifact/23dd148b-1fb0-4219-80e1-53ca8d9d3d97>

It is a **static page with the data baked in**. It makes no live query at view
time, so it only changes when something republishes it.

## How it refreshes

Two scheduled Routines republish it on weekdays:

| Routine | Cron (UTC) | Trigger ID |
| --- | --- | --- |
| `refresh-yusen-artifact-830am-330pm` | `30 12,19 * * 1-5` | `trig_01YG7tbcgDnpBRKkxo1KDHok` |
| `refresh-yusen-artifact-noon-6pm` | `0 16,22 * * 1-5` | `trig_01PrPh79KQSXtmK2fK9MBBVr` |

Each firing:

1. **reads** the live artifact with the Artifact tool (`action: "read"`),
2. clones `americanflat/Ops` and runs
   `tools/yusen_dashboard_refresh.py run --published <the read file>`,
3. **publishes** the rendered HTML back to the artifact URL on `CHANGED`,
4. **re-reads** and runs `verify --published <fresh read file>`, which must
   print `VERIFIED <fp>`.

Step 1 is mandatory for two reasons: a publish from a session that has never
read the artifact is refused as a conflict, and the file that read saves *is*
the pipeline's state (see below). Passing `url:` to the publish is also
mandatory — without it the run mints a duplicate artifact and the real
dashboard goes stale silently.

Step 4 is the only thing that distinguishes "published" from "reported
published". Never treat a green Routine, or a session's own summary, as
evidence the dashboard was republished.

## The live artifact is the state

`refresh_artifact_dashboard.py` (upstream, in
`anthony-amf/americanflat-ops-director`) gates the republish on a fingerprint of
the rendered rows and expects the caller to persist that fingerprint via
`--state`. Persisting it is the part that never worked:

| Store | Outcome |
| --- | --- |
| `.dashboard-state.json` in the upstream repo | scheduled sessions have anonymous read only; the push never landed and state froze at 2026-08-07 |
| `americanflat.observability.pipeline_runs` | write denied, HTTP 403, still denied 2026-09-01; the table is **empty** — nothing was ever recorded |
| a new `finance.dashboard_state` table | `bigquery.tables.create` denied on that dataset |

So the wrapper stores nothing. The rendered page carries every row it displays
in a `const DATA` literal, so the fingerprint of what is *currently published*
is recovered from the published page itself — and the session already has to
read that page before it may publish. No credentials, no table, no git push,
and no state that can drift from reality.

This removed the outstanding ask for a `bigquery.tables.updateData` grant on
`observability`. Nothing in this pipeline needs it.

## Why the gate never closed (2026-09-01)

Two independent faults, both fixed:

**1. No state at all.** Per the table above, every run saw "no previous
publish", so the gate always opened and every firing republished — four
redundant agent sessions per weekday.

**2. The fingerprint could never be stable anyway.** The nightly validation
Routine rewrites `validation_report` with a fresh `[AUTO <today>]` block and
bumps `validated_at` to today on ~76 invoices every night, whether or not any
finding changed. On 2026-09-01, comparing the live page against a fresh render:
359 rows each, 76 rows differing, and **every single difference was a date
string** — no change to a status, an amount, a verdict or a document link. A
raw fingerprint therefore changes every night by construction, so even with a
working state store the gate would have opened daily.

The wrapper now normalizes that churn before hashing: `validated_at` is
dropped, and a `[TAG YYYY-MM-DD]` stamp inside a report is reduced to `[TAG]`.
A genuinely new block still changes the text beyond its date, and any real
change to a status, amount, payment or link still changes the row — so real
changes still republish. Verified on 2026-09-01: normalized live and rendered
fingerprints matched exactly (`b122b16c27fc2742`), correctly yielding
`NO_CHANGE`.

If you change what the dashboard displays, check whether the new field churns
daily. A volatile field inside the fingerprint silently reinstates fault 2.

## Corrections to the earlier diagnosis

Two theories recorded here previously were wrong, and both cost time:

* **"Step 1 fails intermittently; a run of about 60s failed early and about
  170s published."** Step 1 takes **4 seconds** end to end (the upstream clone
  is about 1s). Run duration says nothing about whether a publish happened. The
  48s run on 2026-09-01 and the 35s run on 2026-08-31 both published —
  confirmed by the `artifacts.updated_at` entry on each run's session
  (`2026-09-01T12:54:58Z`, `2026-08-31T22:10:16Z`).
* **"`main` may not carry the tool, so fall back to
  `claude/invoice-dashboard-missing-data-axicr7`."** `main` carries it. All the
  fallback ever proved was that a stale local ref looks like a missing file.
  The prompts keep one fallback clone, but it now guards a real condition —
  whether the checkout carries the current `--published` flow — rather than a
  guess about which branch is good.

The genuine silent-failure history is still worth knowing:

* **2026-08-21 → 08-28.** Both prompts began with `cd
  /home/user/americanflat-ops-director`. Environment
  `env_01VYuZvJjeBFovELdTWzcubw` is sourced from `americanflat/Ops`, which
  clones to `/home/user/Ops`, so the directory did not exist, the `&&` chain
  short-circuited, and the refresh never ran. The prompts now clone a fresh
  checkout instead of assuming a directory.
* **The publish conflict.** Fixing the path was not enough: a publish from a
  session that has never *read* the artifact is refused, and those sessions only
  ever published. The read is now Step 1, and it also carries the state.

Both faults reported `ROUTINE_RUN_STATUS_SUCCEEDED` throughout, because the
session finished cleanly — the prompt told it to report the error and stop, and
it did. That is what Step 4's `verify` exists to catch.

## Not fixed here: the staleness alarm lies

A Slack app (**Invoice Bot**, bot user `U0BCWBLLAQM`) DMs Anthony at 08:00 and
17:15 MT with `⚠️ Yusen dashboard is stale — last successful publish was N hours
ago`. Its N increases monotonically (186h → 219h → 234h → 243h across Aug 29–31),
i.e. the timestamp it reads is **frozen** around 2026-08-21 and has not advanced
since — while the artifact itself was demonstrably republished on Aug 28, Aug 31
and Sep 1.

Whatever it reads, it is not `observability.pipeline_runs` (that table is empty,
so it would have no timestamp at all) and not the artifact's own version. That
bot's code is in neither `americanflat/Ops` nor
`anthony-amf/americanflat-ops-director`; it could not be located from a session,
so its alerts are currently false alarms and should not be treated as evidence.
Fixing it means finding where it is deployed and pointing it at the artifact's
real version timestamp.

## Checking it by hand

Take a fresh read of the artifact (Artifact tool, `action: "read"`, the artifact
URL) and note the file it saves, then:

```
git clone --quiet --depth 1 -b main https://github.com/americanflat/Ops /tmp/ops
python3 /tmp/ops/tools/yusen_dashboard_refresh.py run --published <read file>
```

`NO_CHANGE <fp>` means the live page already shows current data. `CHANGED <path>
<fp>` means publish `<path>` to the artifact URL, then re-read and:

```
python3 /tmp/ops/tools/yusen_dashboard_refresh.py verify --published <fresh read file>
```

Compare against BigQuery directly:

```sql
SELECT MAX(date) AS max_date, COUNT(*) AS n
FROM `americanflat.finance.yusen_invoices`
```

If the dashboard's newest invoice trails `max_date`, the publish step is broken —
not the ingestion.

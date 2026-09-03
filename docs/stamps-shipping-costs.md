# `finance.stamps_shipping_costs` — Stamps.com shipping costs in BigQuery

One row per shipping label bought through Stamps.com, keyed on tracking number.
`amount_paid` is what we actually paid for that label. This is the cost side of
the weekly shipping-cost and cost-per-unit work, which until now has read
Stamps.com data out of CSVs and Google Sheets on every run.

* **Table:** `americanflat.finance.stamps_shipping_costs`
* **Schema:** [`schemas/stamps_shipping_costs.json`](../schemas/stamps_shipping_costs.json)
* **Loader:** [`tools/stamps_shipping_costs_load.py`](../tools/stamps_shipping_costs_load.py)

## Status: the table does not exist yet — it needs one admin command

**The table has not been created.** Creating it needs
`bigquery.tables.create` on `americanflat:finance`, which the warehouse
credential available to Claude Code sessions does not have. Verified three ways
on 2026-09-03:

| Check | Result |
| --- | --- |
| `testIamPermissions` on `americanflat:finance` | grants only `bigquery.tables.get`, `bigquery.tables.list` |
| Same, asking for `tables.create` + `tables.updateData` | returns `{}` — neither granted |
| A real `CREATE TABLE` DDL against `finance` | `HTTP 403 … Permission bigquery.tables.create denied on dataset americanflat:finance` |

`bigquery.jobs.create` *is* granted at project level, so these sessions can
read and query `finance` — they just cannot create or write tables. The same
denial holds on every other dataset in the project (`Demand_Planning`,
`observability`, `warehouse`, `shipstation`, `QC`, `Views`, `marketplaces`).

This is not a misconfiguration to route around. It matches how the rest of
finance already works: `skill-invoice-to-bigquery` deliberately performs no IAM
or schema operations, and its `references/admin_setup.md` makes creating the
table the dataset owner's job. Everything in this directory is built to that
convention — the schema and loader are ready, and the create stays with the
owner.

### Option A — paste this into the BigQuery console (no CLI needed)

Open the dataset in the console and run
[`schemas/stamps_shipping_costs.sql`](../schemas/stamps_shipping_costs.sql) in
the query editor. It is the same table as the `bq mk` form below, with the
column descriptions inline. Its syntax is validated against BigQuery; only the
`tables.create` grant is missing.

<https://console.cloud.google.com/bigquery?ws=!1m4!1m3!3m2!1samericanflat!2sfinance>

### Option B — the command the dataset owner runs (once)

```bash
git clone --depth 1 https://github.com/americanflat/Ops /tmp/ops

bq mk --table \
  --time_partitioning_field=ship_date \
  --clustering_fields=carrier,tracking_number \
  --description="One row per Stamps.com shipping label. amount_paid is the cost of that label." \
  americanflat:finance.stamps_shipping_costs \
  /tmp/ops/schemas/stamps_shipping_costs.json
```

Partitioning on `ship_date` and clustering on `carrier, tracking_number` follow
the pattern `admin_setup.md` recommends: the weekly reports always filter by a
date window, and the join to 3PL orders is on tracking number.

The writer service account already used for `finance.freight_invoices`
(`invoice-writer@americanflat.iam.gserviceaccount.com`) needs `WRITER` on the
dataset, which it has. No new service account or IAM grant is required — the
loader writes through that same identity.

## Loading data

The loader has two modes. `prepare` needs no credentials and touches nothing in
the cloud, so it is always safe to run first.

```bash
# Parse and check the numbers. Writes NDJSON, no cloud access.
python3 tools/stamps_shipping_costs_load.py prepare PrintHistory_*.csv

# Load, impersonating the writer service account.
python3 tools/stamps_shipping_costs_load.py load PrintHistory_*.csv
```

`load` requires the Google Cloud SDK on PATH and `gcloud auth login`; without
it the loader says so and exits rather than failing obscurely. It never creates
datasets, tables or IAM bindings — if the table is missing or impersonation is
denied, it prints who to ask.

### Re-running a file is safe

Every row carries the `source_file` it came from and the `ingested_at` of the
run that wrote it. A load appends the new generation first, then deletes that
filename's older rows. So a corrected re-export replaces exactly its own rows
and never doubles them, and loading a *different* file never touches another
file's rows.

The order matters: appending first means a failed load deletes nothing. If the
cleanup step fails instead, the table holds duplicates for that one file but
has lost nothing, and re-running the load resolves them. The loader says so
explicitly and exits non-zero rather than reporting success.

This is why `source_file` and `ingested_at` are on every row rather than being
dropped as noise.

## Where the source data lives

The canonical consolidated source is the Google Sheet **"AMF Stamps.com
Invoices"** (`1z9phKaTD2LZygyoJKHOrvzNHqjPJr8zgfVHB37i60Pk`, in Anthony's
Drive), built by appending Stamps.com PrintHistory exports. Its columns are:

```
Ship Date | Tracking Number | Carrier | Service | Weight | Amount Paid | To Name | To ZIP | Source File
```

Raw exports come from Stamps.com → Reporting → Shipment History → Export CSV,
and land as `PrintHistory_<account>_<from>_to_<to>.csv`. The loader reads
either shape: it matches columns on a normalized header, so the raw export's
`Tracking #` / `Adjusted Amount` / `Order ID` / `Reference 1` / `Cost Code` and
the sheet's `Tracking Number` both map to the right columns without editing the
file.

Prefer loading the **raw exports**. The consolidated sheet drops `Order ID`,
`Reference 1` and `Cost Code`, and leaves `Weight`, `To Name` and `To ZIP`
blank — and those reference fields are the join keys to 3PL shipped-order
reports that make marketplace cost attribution possible. The schema keeps
columns for them so raw loads populate them; sheet-shaped loads simply leave
them null.

## Gotchas

**`amount_paid` can be negative.** Refunds and postage corrections post as
negative rows (`-0.14`, `-1` are both real). They are preserved, not clamped —
they are genuine credits and dropping them overstates cost. Some rows are
exactly `0`. Any query summing cost should keep them; any query counting
*labels shipped* should not treat a negative row as a shipment.

**Money is `NUMERIC`, not `FLOAT`.** Cost-per-unit figures get summed across
tens of thousands of labels; binary floats drift. The loader passes amounts as
exact decimal strings.

**`to_zip` is `STRING`.** `02108` is not `2108`.

**`Weight` has two formats.** Either decimal pounds (`2.5`) or `Xlb Yoz`
(`1lb 5oz`). The loader normalizes both to pounds — `1lb 5oz` → `1.3125`. It is
frequently blank in the consolidated export.

**Stamps.com is the label vendor, not the carrier.** `carrier` is `USPS` or
`UPS`. Do not read "Stamps" as a carrier when comparing against FedEx.

**Rows with no tracking number or no amount are skipped**, and `prepare`
reports how many. A label with no cost carries no financial signal and would
drag any cost-per-unit average toward zero.

## Querying it

```sql
-- Weekly Stamps.com spend and average cost per label
SELECT
  DATE_TRUNC(ship_date, WEEK(MONDAY)) AS week,
  carrier,
  COUNT(*)                            AS labels,
  SUM(amount_paid)                    AS total_cost,
  AVG(amount_paid)                    AS avg_cost_per_label
FROM `americanflat.finance.stamps_shipping_costs`
WHERE ship_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY week, carrier
ORDER BY week DESC, carrier;
```

```sql
-- Refunds and corrections only
SELECT ship_date, tracking_number, carrier, amount_paid
FROM `americanflat.finance.stamps_shipping_costs`
WHERE amount_paid < 0
ORDER BY ship_date DESC;
```

Duplicate tracking numbers across files are worth watching — the same label
should not appear in two PrintHistory exports:

```sql
SELECT tracking_number, COUNT(*) AS n, STRING_AGG(DISTINCT source_file) AS files
FROM `americanflat.finance.stamps_shipping_costs`
GROUP BY tracking_number
HAVING n > 1
ORDER BY n DESC;
```

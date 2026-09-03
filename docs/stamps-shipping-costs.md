# `finance.stamps_shipping_costs` — Stamps.com shipping costs in BigQuery

One row per shipping label bought through Stamps.com, keyed on tracking number.
`amount_paid` is what we actually paid for that label. This is the cost side of
the weekly shipping-cost and cost-per-unit work, which until now has read
Stamps.com data out of CSVs and Google Sheets on every run.

* **Table:** `americanflat.finance.stamps_shipping_costs`
* **Schema:** [`schemas/stamps_shipping_costs.json`](../schemas/stamps_shipping_costs.json)
* **Loader:** [`tools/stamps_shipping_costs_load.py`](../tools/stamps_shipping_costs_load.py)

## Status: table created 2026-09-03

The table exists, partitioned by `ship_date` (DAY) and clustered on
`carrier, tracking_number`, with `tracking_number` REQUIRED. It was created by
running [`schemas/stamps_shipping_costs.sql`](../schemas/stamps_shipping_costs.sql)
in the BigQuery console query editor.

If it ever needs rebuilding, that file is the source of truth — one paste into
the query editor, or via the SDK:

```bash
bq mk --table \
  --time_partitioning_field=ship_date \
  --clustering_fields=carrier,tracking_number \
  americanflat:finance.stamps_shipping_costs \
  schemas/stamps_shipping_costs.json
```

Do not use the console's **Create table** form for this. Its "Edit as text"
schema box accepts only `name:type` — it rejects the `:REQUIRED` mode suffix,
so `tracking_number` comes out nullable, and BigQuery cannot tighten a nullable
column to required afterward. The form also drops every column description and
makes the partition and cluster settings easy to miss. The DDL carries all
three correctly.

### Claude Code sessions cannot write this table

Worth knowing before automating anything against it: the warehouse credential
available to Claude Code sessions is **read-only**. It holds
`bigquery.tables.get` and `bigquery.tables.list` on `finance` and nothing more
— no `tables.create`, no `tables.updateData` — and the same denial holds across
all 60 datasets in the project. Project-level `bigquery.jobs.create` *is*
granted, so sessions can query the table; they cannot load it.

So the load runs from an operator machine with the Google Cloud SDK and a
`gcloud auth login`, writing through
`invoice-writer@americanflat.iam.gserviceaccount.com` — the same identity
`finance.freight_invoices` already uses. This matches
`skill-invoice-to-bigquery`, which also performs no IAM or schema operations by
design.

### The overlapping re-export trap

Stamps.com re-exports overlapping ranges under new filenames, and a Downloads
folder accumulates them. A real run on 2026-09-03 over 18 files found **28,330
repeated tracking numbers**. So the loader dedupes on `tracking_number` within
a run, keeping the last occurrence and reporting what it collapsed.

**Prefer one comprehensive export over many weekly ones.** In that same run,
`PrintHistory_1003346887_4.30.2026_to_8.31.2026.csv` held all 20,528 labels for
2026-04-30 → 2026-08-31 by itself. Every weekly file was a strict subset,
contributing not one unique label — deduping 18 files gave exactly the same
20,528 labels and the same carrier split as that one file alone.

**But the totals differed, and the wide file was right.** Deduped across 18
files: $235,644.87. The one comprehensive file: $239,109.04 — $3,464.17 (1.45%)
more, for the identical set of labels. Two things explain it, and both say the
comprehensive export is authoritative:

* Its 40 negative rows (refunds and postage corrections) appear in **no**
  weekly file. Those weeklies were exported before the credits posted.
* Carrier reweighs and rerates adjust charges *upward* after the fact. A
  comprehensive export taken later carries them; a weekly taken near the ship
  date does not.

**Why the glob got it backwards.** `PrintHistory_*.csv` expands
**alphabetically**, which orders by the date range *in the filename*, not by
when the file was exported. A wide backfill export sorts early precisely
because its range starts earliest — while being the newest and most adjusted.
Last-wins then overwrites its post-audit amounts with stale weekly ones and
understates cost. Label counts stay identical, so nothing looks wrong; only the
money moves.

So `prepare` now prints the cost under last-wins *and* under first-wins, and
flags what the file ordering is worth:

```
  cost with last occurrence kept   $235,644.87  <- loading this
  cost with first occurrence kept  $239,109.04
  file order is worth $3,464.17 (lower as ordered)
```

Any non-zero figure there means your file order is deciding real money. Check
it before loading. If one export already covers the whole period, load only
that file and the ambiguity disappears.

The other limitation still holds: dedupe only sees files passed to the *same*
invocation. Two separate loads of differently-named overlapping exports will
both land — run the duplicate query below after any backfill.

There are also multiple Stamps.com accounts in play (`1003346887`,
`70000000002603580`, `5000029007`). All belong in the table; only `source_file`
distinguishes them, so add an account column if that ever matters.

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

# Carrier shipping invoices → BigQuery

Weekly Stamps.com, FedEx and UPS invoices land in
`americanflat.finance.shipping_invoices`, one row per charge line, so shipping
cost can be queried directly instead of re-opening spreadsheets.

* Table + view DDL: `sql/finance_shipping_invoices.sql`
* Loader: `tools/shipping_invoice_load.py`
* Tests: `python3 tools/test_shipping_invoice_load.py`

## One-time setup (needs an admin)

Run `sql/finance_shipping_invoices.sql` once in the BigQuery console. It creates
the table and the `finance.shipping_invoice_packages` roll-up view.

This step cannot be done from an ops session: the warehouse credential those
sessions use can read and run DML on `finance`, but `bigquery.tables.create` is
denied on every dataset in the project (verified 2026-09-01 — the same wall
`docs/yusen-dashboard-refresh.md` hit with `finance.dashboard_state`). Once the
table exists the loader works with no further grant, because appending only
needs `bigquery.tables.updateData`, which the credential already has.

## Weekly run

```bash
# 1. Look at the money first. Parses everything, writes nothing.
python3 tools/shipping_invoice_load.py review ~/invoices/2026-08-24/

# 2. Append once the totals match the carrier statements.
python3 tools/shipping_invoice_load.py load ~/invoices/2026-08-24/
```

Both commands take individual files or a directory. `review` prints, per file,
the carrier it detected, the row count, the total charged and the ship-date
span it covers — compare that total against the invoice before loading.

The files themselves are the same ones the `download-weekly-shipping-reports`
skill already stages each Thursday: the FedEx invoice CSV, the Stamps.com print
history CSV, and the UPS Billing Center CSV. Export UPS **with headers** — the
headerless fixed-column format is not parsed (`--carrier UPS` will not rescue
it; the columns are unlabelled).

Useful flags:

| Flag | Why |
| --- | --- |
| `--carrier FedEx\|UPS\|Stamps.com` | Force the carrier when header detection fails |
| `--replace` | Reload files already loaded — deletes their rows first |
| `--allow-duplicate` | Append anyway (a carrier reissuing the same filename) |
| `--ndjson out.json` | Dump the parsed rows to look at before loading |

Loading the same filename twice is refused by default. The guard keys on the
CSV's basename, so keep the carrier's filenames — don't rename them to
something generic like `invoice.csv` each week.

## What the rows look like

Grain is **one row per charge line**, because that is how the carriers bill:

* **FedEx** splits a package into tracked charges — transportation, fuel,
  residential, additional handling — so one package becomes several rows. The
  invoice's own net charge for that package is kept in `raw` so the lines stay
  reconcilable against what was printed.
* **UPS** already bills one row per charge; those pass through 1:1.
* **Stamps.com** bills one flat amount per label, so one label is one row, with
  `charge_description = 'Postage'`.

`charge_category` buckets each line to `base` / `fuel` / `surcharge` /
`adjustment` / `other`, which is what makes "how much of our spend is fuel"
answerable across three carriers that name the same fee differently.

To join against 3PL shipped orders, use the package-level view — 3PL reports
are keyed on tracking, not on charge line:

```sql
SELECT * FROM `americanflat.finance.shipping_invoice_packages`
WHERE ship_date >= '2026-08-18'
```

### Reference columns

The marketplace usually has to be recovered from a reference field, and each
carrier puts it somewhere different:

| Column | FedEx | UPS | Stamps.com |
| --- | --- | --- | --- |
| `reference_1` | Original Customer Reference | Shipment Reference Number 1 | Reference 1 |
| `reference_2` | Original Ref#2 | Shipment Reference Number 2 | **Printed Message** |
| `reference_3` | Original Ref#3/PO Number | — | — |
| `order_id` | — | — | Order ID |

`reference_3` is the PO that `shipping-cost-report`'s 3PL match runs on.
Stamps.com print history carries no invoice number, so `invoice_number` is NULL
for those rows — it is a print export, not a bill.

## When a carrier changes their file format

Nothing breaks. The parser maps headers it recognises onto the canonical
columns and drops **every unrecognised column** into `raw` as JSON, so an
unfamiliar column still lands losslessly and stays queryable:

```sql
SELECT JSON_VALUE(raw, '$.some_new_column') FROM `americanflat.finance.shipping_invoices`
```

Promoting a new column to first class means adding an alias to
`COMMON_ALIASES` in the loader — no schema change, unless it deserves its own
column, in which case add it to the DDL *and* to `SCHEMA` in the loader (the
test asserts the two stay in step).

Header matching is punctuation- and case-insensitive: `Original Ref#3/PO Number`,
`ORIGINAL REF#3 / PO NUMBER` and `original_ref_3_po_number` all match.

## Example queries

```sql
-- Weekly spend by carrier
SELECT DATE_TRUNC(ship_date, WEEK(MONDAY)) AS week, carrier,
       SUM(amount) AS spend, COUNT(DISTINCT tracking_number) AS packages
FROM `americanflat.finance.shipping_invoices`
WHERE ship_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY week, carrier ORDER BY week DESC, carrier;

-- Where the surcharges are going
SELECT carrier, charge_description, SUM(amount) AS spend, COUNT(*) AS lines
FROM `americanflat.finance.shipping_invoices`
WHERE charge_category = 'surcharge'
  AND ship_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
GROUP BY carrier, charge_description ORDER BY spend DESC LIMIT 20;

-- Packages billed above their actual weight (DIM-rated)
SELECT carrier, tracking_number, weight_lbs, billed_weight_lbs, total_amount
FROM `americanflat.finance.shipping_invoice_packages`
WHERE billed_weight_lbs > weight_lbs + 0.5
ORDER BY total_amount DESC LIMIT 50;

-- What has been loaded, and from which file
SELECT source_file, carrier, COUNT(*) AS rows_, SUM(amount) AS total,
       MIN(ship_date) AS from_, MAX(ship_date) AS to_, MAX(ingested_at) AS loaded
FROM `americanflat.finance.shipping_invoices`
GROUP BY source_file, carrier ORDER BY loaded DESC;
```

## Limits worth knowing

* Rows whose ship date cannot be parsed (account-level fees, weekly service
  charges) land in the table's NULL partition. `review` counts them for you.
* Account-level charges have no tracking number and so never appear in the
  package view — query the base table for a complete spend total.
* The loader has no scheduler. It runs when someone runs it, right after the
  weekly files are staged.

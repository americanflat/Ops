# `finance.customs_entries` — customs duty (CBP Form 7501) in BigQuery

One row per customs entry, parsed from the 7501 entry summary the customs
broker (Worldwide Logistics CHB) sends as the duty bill. Box 44 is what
Americanflat owes CBP for the entry. Lines, HTS rows, fees and the commercial
invoice reconciliation are nested in the row; every entry is validated before
it loads, and what the validator found is stored with it.

This is the customs leg of the freight bills — drayage and demurrage are next,
and will join on `bill_of_lading` (and container number).

* **Table:** `americanflat.finance.customs_entries` (partitioned by `entry_date` month,
  clustered on `entry_number, bill_of_lading`)
* **Flat view:** `americanflat.finance.customs_entry_tariffs` — one row per HTS row
* **Schema:** [`schemas/customs_entries.json`](../schemas/customs_entries.json)
* **DDL:** [`sql/finance_customs_entries.sql`](../sql/finance_customs_entries.sql) (table),
  [`sql/finance_customs_entry_tariffs.sql`](../sql/finance_customs_entry_tariffs.sql) (view)
* **Loader:** [`tools/customs_entries_load.py`](../tools/customs_entries_load.py)
* **Tests:** `python3 tools/test_customs_entries_load.py` (fixture: entry 8NW-1591298-8)

## Status: built 2026-10-21, table not created yet

Creating the table needs `bigquery.tables.create` on `finance`, which the
cloud ops session's credential doesn't have (same wall as
[`yusen-dashboard-refresh.md`](yusen-dashboard-refresh.md)). Run once from an
operator machine signed in as an account that can — as of 2026-10-19,
`anthony@americanflat.com` can:

```bash
python3 tools/customs_entries_load.py --init
```

or paste `sql/finance_customs_entries.sql` then `sql/finance_customs_entry_tariffs.sql`
into the BigQuery console.

## Loading entries

Needs the Google Cloud SDK (`gcloud auth login`) and poppler's `pdftotext`
(`brew install poppler`). Writes as the signed-in account, no service-account hop.

```bash
# Parse and validate. No cloud access, writes nothing.
python3 tools/customs_entries_load.py --dry-run ~/Downloads/Bill_*-*_INVOICE_*.pdf

# Load (same files).
python3 tools/customs_entries_load.py ~/Downloads/Bill_*-*_INVOICE_*.pdf
```

The dry run prints every HTS row, the fees, boxes 39–44 and each finding.
An already-extracted `.txt` (from `pdftotext -layout`) is accepted in place of a PDF.

* **Same PDF twice** — skipped (matched on entry number + SHA-256).
* **Different PDF, same entry** (a corrected 7501 / post summary correction) —
  refused; `--replace` appends the new version and then deletes the old one.
* **Post-load verify** — re-reads each entry: exactly one row, same box 44 total, or exit 6.

## What the validator checks

| Check | Severity | Means |
|---|---|---|
| `tariff_duty` | error | An HTS row's duty ≠ rate × value. Chapter 99 rows (301, IEEPA, 232) print value 0 and are checked against the line's commodity value |
| `line_fee` | error | A line's MPF/HMF ≠ rate × line value |
| `duty_total` | error | HTS duties don't sum to box 41 |
| `fee_summary` | error | Fee summary ≠ the line fees. MPF is clamped to that fiscal year's min/max first |
| `other_total`, `grand_total` | error | Fees ≠ box 43; 41+42+43 ≠ box 44 |
| `entered_value_total` | **flag** | Line values don't sum to box 39 (whole-dollar rounding allowed). Message estimates the duty on the declared value |
| `invoice_entered_value` | flag | The commercial invoice reconciles to a different entered value than box 39 |
| `duty_deduction` | **flag** | The commercial invoice deducted a different amount of duty than this entry charged |
| `freight_charges` | flag | Line charges (box 36B) ≠ the invoice's international-freight deduction |
| `mpf_limits` | info | No MPF min/max on file for the entry's fiscal year — MPF not checked |

**Errors** mean the form doesn't add up. A CBP-generated form always does, so
an error almost always means the parse went wrong (a new layout, a wrapped
line); the entry is refused. Look at the dry-run table before reaching for
`--allow-mismatch`.

**Flags** mean the form adds up but the entry looks wrong. Flagged entries
load with `validation_status = 'flagged'` — they're the ones to take to the broker.

### Keep `MPF_LIMITS` current

CBP resets the MPF minimum and maximum every fiscal year (Oct 1). The table at
the top of the loader has FY2025 and FY2026; add FY2027 when CBP publishes it.
Until then FY2027 entries (entry date on or after 2026-10-01) get an `info`
finding and their MPF isn't checked.

## First finding: entry 8NW-1591298-8 (2026-10-21)

HLCUNG12607TYYG2, 850 cartons of frames from CN, entered 2026-09-20, $643.95.
The form's arithmetic is clean, but:

* the three lines' values sum to **$2,575**, while box 39 declares **$11,745**
  (and the commercial invoice reconciles to $11,744.59); duty was charged on
  the $2,575 — about **$2,769** at the same rates on $11,745, vs **$607.15** paid;
* the commercial invoice deducts **$2,752.46** of duty, fees and taxes —
  close to that $2,769 — against $643.95 actually charged.

Americanflat is importer of record, so an underpayment is ours. Ask the
broker for an explanation before calling it an error.

## Querying it

```sql
-- Flagged entries and why
SELECT entry_number, entry_date, bill_of_lading, total, f.check_name, f.message
FROM `americanflat.finance.customs_entries`, UNNEST(validation_findings) f
WHERE f.severity = 'flag'
ORDER BY entry_date DESC;

-- Duty by HTS heading, chapter 99 surcharges split out
SELECT is_chapter_99, SUBSTR(hts_number, 1, 4) AS heading, SUM(duty) AS duty
FROM `americanflat.finance.customs_entry_tariffs`
GROUP BY 1, 2 ORDER BY duty DESC;

-- What each container cost to clear
SELECT bill_of_lading, SUM(total) AS owed, SUM(duty) AS duty, SUM(other_fees) AS fees
FROM `americanflat.finance.customs_entries`
GROUP BY 1;
```

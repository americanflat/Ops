# `finance.fedex_shipping_costs` and `finance.ups_shipping_costs` — carrier invoices in BigQuery

One row per invoice line from the weekly FedEx and UPS invoice files. These sit
beside [`finance.stamps_shipping_costs`](stamps-shipping-costs.md) so all three
carrier bills that feed the shipping-cost and cost-per-unit work live in one
dataset.

* **Tables:** `americanflat.finance.fedex_shipping_costs`, `americanflat.finance.ups_shipping_costs`
* **Load logs:** `finance.fedex_shipping_costs_load_log`, `finance.ups_shipping_costs_load_log`
  (one row per file loaded; a file already logged as `loaded` is skipped)
* **Schemas:** [`schemas/fedex_shipping_costs.json`](../schemas/fedex_shipping_costs.json),
  [`schemas/ups_shipping_costs.json`](../schemas/ups_shipping_costs.json),
  [`schemas/carrier_invoice_load_log.json`](../schemas/carrier_invoice_load_log.json) (both logs)
* **Loader:** [`tools/carrier_invoices_load.py`](../tools/carrier_invoices_load.py) — detects the carrier
  from the file's header, so FedEx and UPS files can be passed together.

| Table | Source file | Amount column | Ties to |
|---|---|---|---|
| `fedex_shipping_costs` | FedEx Billing Online invoice CSV (`FedEx_invoice_*.CSV`) | `net_charge` | `invoice_amount_due` per invoice |
| `ups_shipping_costs` | UPS billing-data CSV v2.1 (`Invoice_000000<acct><nnn>_<mmddyy>.csv`) | `net_amount` | `invoice_amount` per invoice |

Both tables are partitioned by `invoice_date` (MONTH). FedEx is clustered on
`invoice_number, tracking_id`; UPS on `invoice_number, tracking_number`. Every
row keeps the full original line in `raw` (JSON), plus `source_file`,
`source_sha256`, `loaded_at`, `loaded_by`.

## Status: created and backfilled 2026-10-19

* **FedEx:** invoices 2026-01-05 → 2026-10-19, every week · 149 invoices · 20,802 lines ·
  $553,505.05. (The 2026-09-21 week came in later the same day from a full-September export.)
* **UPS:** invoices 2026-09-05 → 2026-10-17 · 10 invoices · 11,739 lines · $13,909.68.
  H9229J (SC) is weekly; F20K19 (NJ) only has 9/5, 9/12, 10/10 — the other NJ
  weeks were either $0 or not downloaded (unconfirmed).

The UPS accounts here are Americanflat's own (H9229J, F20K19). Marketplace
orders that show up on them are warehouse errors billed back to Yusen; see the
UPS weekly invoice → Yusen credit process.

## Loading data

Runs from an operator machine with the Google Cloud SDK and `gcloud auth login`.
It writes **as the signed-in account** — no service-account hop. (As of
2026-10-19 `anthony@americanflat.com` can create and write tables in `finance`
directly, while impersonating `invoice-writer@` was denied, which is also why
the Stamps loader was run with `--no-impersonate` that day.)

```bash
export PATH="$HOME/google-cloud-sdk/bin:$PATH"
cd ~/Ops

# Parse and check every invoice's lines against its stated total. No cloud access.
python3 tools/carrier_invoices_load.py --dry-run ~/Downloads/FedEx_invoice_*.CSV ~/Downloads/Invoice_000000*.csv

# Load (same files). Already-loaded files are skipped.
python3 tools/carrier_invoices_load.py ~/Downloads/FedEx_invoice_*.CSV ~/Downloads/Invoice_000000*.csv
```

Globbing is fine here, unlike Stamps: invoice lines never change after an
invoice is issued, so file order cannot change the result. Leave out the
`*_by_order.csv` files that `ups_invoice_breakdown.py` writes — they are
summaries, not invoices (the loader would skip them anyway, since their header
isn't UPS billing data).

`--init` creates any missing carrier or log table (never the dataset). Only
needed if a table is ever rebuilt.

### What it checks, and when it refuses

1. **Invoice ties.** Each invoice's line sum must equal the total printed in
   the file, or that file is refused (`--allow-mismatch` overrides).
2. **Same file twice.** A file whose SHA-256 is already logged as `loaded` is
   skipped (`--force` re-checks it; still writes nothing new if every line is
   already present).
3. **Same lines, different export.** Lines are matched on `row_key`, so a
   re-download of an already-loaded invoice adds nothing.
4. **Invoice overlap guard.** If a file has lines the table doesn't recognize
   for an invoice that's *already loaded*, the whole file is refused before
   writing. An issued invoice never gains lines, so this means the key failed
   to recognize a re-export. `--allow-invoice-overlap` overrides; don't use it
   without understanding why the keys differ.
5. **Post-load verify.** After the MERGE, the file's row count and dollar total
   are re-read from the table; a mismatch is logged as `verify_failed`.

### The FedEx column-order trap (2026-10-19)

FedEx invoice CSVs carry 51 repeated `Tracking ID Charge Description` /
`Tracking ID Charge Amount` pairs and 4 `Commodity Description` /
`Commodity Country/Territory Code` pairs, and **the order of those pairs is not
stable between downloads**. Two overlapping Jan–Sep exports listed the same
847 June lines ($20,813.90) with Fuel Surcharge and Discount in different slots.
A key built from the raw line saw them as different lines and would have
double-counted June.

`row_key` for FedEx is therefore built from the line's non-repeated columns plus
each pair group as an *unordered set* (amounts normalized to cents), then an
occurrence index so genuinely identical lines inside one invoice both survive.
Existing rows were re-keyed in place the same day (19,732 rows, totals
unchanged), and the untrimmed overlapping export was re-run as a test: 0 new,
10,277 already present.

### The paid-invoice trap (also 2026-10-19)

FedEx's `Current Balance` column is what's still owed on the invoice, repeated
on every line, and it drops to `0.00` once the invoice is paid. A full-September
export downloaded after the 9/28 invoices were paid therefore looked like 1,029
new lines on already-loaded invoices (~$25.6K). The invoice overlap guard
refused it and nothing was written. `Current Balance` is now left out of the
FedEx key (`FEDEX_UNSTABLE`); existing rows were re-keyed again (19,732 rows,
totals unchanged) and the export then loaded the missing 9/21 week only:
1,070 new, 3,186 already present. `Original Amount Due` is the invoice total and
doesn't change, so it stays in the key. If FedEx ever adds another
status-style column, add it to `FEDEX_UNSTABLE` the same way — the guard will
name the invoices when it happens.

UPS billing data has fixed column positions, so UPS keys still hash the raw line.

## Querying it

```sql
-- Weekly FedEx spend by origin
SELECT DATE_TRUNC(shipment_date, WEEK(MONDAY)) AS week, shipper_city,
       SUM(net_charge) AS net
FROM `americanflat.finance.fedex_shipping_costs`
GROUP BY 1, 2 ORDER BY 1, 2;

-- UPS by account and invoice
SELECT account_number, invoice_number, invoice_date, SUM(net_amount) AS net
FROM `americanflat.finance.ups_shipping_costs`
GROUP BY 1, 2, 3 ORDER BY 3;
```

Sum `net_charge` / `net_amount` across lines; don't sum `invoice_amount_due` /
`invoice_amount`, which repeat the whole-invoice total on every line.

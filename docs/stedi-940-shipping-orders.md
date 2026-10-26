# `warehouse.shipments_940_raw` — Stedi 940 warehouse shipping orders

One row per 940 sent through Stedi to a warehouse (Taylored Fontana / NJ / SC,
Yusen NL), with its W01 lines nested in `lines`. The 945 ship confirmations
have been in BigQuery for a while (`warehouse.shipments_945`); the 940s were
not, so an order the warehouse never confirmed had no marketplace anywhere in
the warehouse data. The FedEx invoice validator
(`tools/fedex_invoice_validate.py`) is the first consumer.

* **Table:** `americanflat.warehouse.shipments_940_raw`
* **View:** `americanflat.warehouse.shipments_940` (adds `channel`)
* **DDL:** [`schemas/shipments_940_raw.sql`](../schemas/shipments_940_raw.sql) (table + view)
* **Loader:** [`tools/stedi_940_load.py`](../tools/stedi_940_load.py)

## Where the marketplace is

Every 940 names its marketplace in **`N9*IT`**. `N9*DP` is filled only by
Target, so it cannot be relied on. Sampled 2026-10-26 across all four
partnerships:

| `N9*IT` (`marketplaceRaw`) | `channel` | W05-02 depositor | W05-03 PO |
| --- | --- | --- | --- |
| `ACNDA_TGTPL` (+ `N9*DP` Target) | TARG | Target PO | Target PO |
| `AMAZON.COM` | AMZC | DF PO | — |
| `WAYFAIR LLC` | WAYF | CS… PO | — |
| `SHOPIFY` | SHIPSTATION | Shopify order # | Shopify order id (FedEx Ref#3) |
| `WALMARTCOM` | WALC | customer order # | PO |
| `MACYS` | MACY | order # | order #-A |
| `AMAZON.FBA` / `AMAZON.VC` | AMZFBA / AMZVC | shipment id | shipment id |

The IT → channel mapping lives in the view, not the table, so it can change
without reloading. Keep `CHANNEL_940` in `tools/fedex_invoice_validate.py` in
step with it.

## Creating the table (once)

Paste `schemas/shipments_940_raw.sql` into the BigQuery console query editor.
It creates the table (partitioned by `DATE(processedAt)`, clustered on
`depositorOrderNumber, purchaseOrderNumber`) and the view. Claude Code sessions
cannot do this: their BigQuery credential is read-only (see
`docs/stamps-shipping-costs.md`). `warehouse` grants WRITER to project writers.

## Loading

Needs the Google Cloud SDK (`gcloud auth login`) and a Stedi API key
(`STEDI_API_KEY`, or `STEDI_API_KEY_FILE` — the same key
`skill-marketplace-and-edi-recon` uses).

```bash
# Read-only: pull 940s from Stedi into a local file and print counts.
python3 tools/stedi_940_load.py fetch --since 2026-09-01 --out stedi_940_rows.jsonl

# First load: a window, or a file from `fetch`.
python3 tools/stedi_940_load.py load --since 2026-01-01
python3 tools/stedi_940_load.py load --from-file stedi_940_rows.jsonl

# Every later run: resumes from the table's newest processedAt minus a day.
python3 tools/stedi_940_load.py load
```

`load` stages into `warehouse.shipments_940_stage` and MERGEs on
`transactionId`, so overlapping windows update in place and never duplicate.
It then checks rows == distinct transactionIds and fails loudly if not.

Volume is roughly 450 940s a day across the four partnerships. Stedi's list
call returns 100 per page (~5 s each, and the partnership filter 504s now and
then, which the loader retries), plus one JSON artifact per 940 fetched eight at
a time. A month is a few minutes; a full year is a couple of hours.

A 940 can be resent, or sent to a second warehouse on a redirect, so one order
can have several rows. Pick the latest per order with `processedAt`.

## Not done yet

* **Scheduling.** The 945s arrive by a daily batch (~06:03 PT) whose loader is
  not in this repo. The 940 load should run on the same cadence, ideally in
  that same pipeline; until then `load` with no `--since` is the incremental run.
* **`warehouse.channel_keys` / `finance.shipment_reconciliation`** still
  resolve channels from marketplace order tables only. Adding the 940's
  `channel` as a fallback there would resolve more 945 orders for every
  consumer, not just FedEx.

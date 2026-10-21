# Freight & customs invoice validation — design

How freight-side invoices (customs entries, ocean freight, drayage, demurrage /
per diem) get from the broker's email into BigQuery, and what it takes to
validate them beyond "the numbers add up". Status: design, 2026-10-21. Built
so far: [`finance.customs_entries`](customs-entries.md) and its arithmetic checks.

## Requirements from the invoice approver

From the single approver of these invoices:

> I think the validator will work for most invoice types, but CBP entries need
> more data to check properly. It can verify the math, but to judge whether
> the value and rates are right it needs the tariff rates in force on each
> entry date (they change often), the factory invoices, and the shipment type,
> since Amazon DI entries are valued differently from stock. On the freight
> side, LCL and FCL invoices are also built differently, so it would need
> rules for both.

So validation runs at three levels, and each level needs more reference data
than the one before:

| Level | Question | Needs | Status |
|---|---|---|---|
| 1. Arithmetic | Does the document add up? | The document alone | **Built** for 7501s (`tariff_duty`, `duty_total`, `fee_summary`, `grand_total`…) |
| 2. Internal consistency | Do the document's own figures agree with each other? | The document alone | **Built** for 7501s (`entered_value_total`, `duty_deduction`, `freight_charges`) |
| 3. Correctness | Is the value right, the rate right, the charge owed? | Reference data: tariff rates by date, factory invoices, shipment type, freight rate sheets, LCL/FCL rules | **Not built** — this document |

Level 3 findings are only as good as the reference data they use, so each
data source below is a prerequisite.

## Data flow (no dependency on the Administrator@ mailbox)

```
Administrator@ (catch-all)                      anyone, any session
┌───────────────────────────┐   hourly   ┌──────────────────────────────────┐
│ Apps Script (runs as      │──────────▶ │ Drive: "Freight Invoices/"        │
│ Administrator@, set up    │            │   Customs-7501/  WWL-Freight/     │
│ once)                     │            │   Per-Diem/  Commercial-Invoices/ │
│  · searches broker mail   │            │   Rate-Sheets/                    │
│  · saves PDFs to Drive    │──────────▶ │ Sheet: invoice_manifest           │
│  · logs every message     │            │   (msg id, type, entry no., BL,   │
│  · labels ops/exported    │            │    container, amount, file links) │
└───────────────────────────┘            └────────────────┬─────────────────┘
                                                          │ Drive for desktop / Drive connector
                                                          ▼
                                      tools/*_load.py  ──▶  BigQuery finance.*
                                      (parse + level 1–3 checks)
```

The script only ever *copies* — it never sends, deletes or moves mail. After
the one-time setup nobody needs to sign in to Administrator@ again. A Google
Group (e.g. `customs-docs@`) that the brokers copy on every document is a
useful backstop for new mail, but not required.

## Reference data for customs entries

### Tariff rates by date — `finance.tariff_rates` (to build)

Rates on the 7501 are the broker's claim. To check them, look up the rate in
force for that HTS number, origin and date.

* One row per (hts_number, country_of_origin, rate program, effective_from,
  effective_to): ad valorem rate, specific rate if any, and the authority
  (HTSUS revision, Federal Register notice).
* Chapter 99 programs (Section 301 lists, IEEPA, 232) are stacked on top of
  the commodity line, so each program is its own row, and the check is "the
  set of 99 lines on this entry line = the set in force".
* **Which date applies differs by program**: most rates go by entry date, but
  some actions exempt goods by export or loading date (in-transit exemptions).
  The 7501 has entry (box 7), import (11) and export (15) dates; store the
  rule per program rather than assume the entry date.
* Source: the USITC HTS revisions (each revision is downloadable), loaded per
  revision with its effective date. Only the HTS numbers Americanflat actually
  imports need to be tracked — the `customs_entry_tariffs` view gives that list.

Checks enabled: `rate_in_force` (rate on the 7501 matches the table for the
applicable date), `program_missing` / `program_extra` (a chapter 99 line
should be there and isn't, or vice versa).

### Factory (commercial) invoices — `finance.factory_invoices` (to build)

The 7501 lists the commercial invoice (e.g. `001/KMLT10036`) and, on the
continuation sheet, its value net of freight and duty. To judge entered value
we need the invoice itself.

* Source: sourcing sends "shipping documents" (commercial invoice + packing
  list) to the broker in reply to each `[Action Required] <BL> - <container>`
  email. The Apps Script saves those attachments to `Commercial-Invoices/`.
* Grain: one row per invoice, with lines (PO, SKU, description, qty, unit
  price, amount), incoterm and currency.
* Checks enabled: `entered_value_vs_invoice` (box 39 vs invoice value adjusted
  for the incoterm and the shipment type rule below), `classification_drift`
  (the same SKU entered under different HTS numbers on different entries),
  `po_unmatched` (PO on the entry with no factory invoice).

### Shipment type — `customs_entries.shipment_type` (column added)

`stock` (Americanflat's own inventory) vs `amazon_di` (Amazon Direct Import).
Per the approver, **these are valued differently**, so the entered-value check
must branch on it. The exact valuation rule for each type is to be written
down with the approver and the broker before that check is built — it is not
inferred here.

* Signals in the data: Amazon DI shipments appear as
  `Americanflat Corp.-MPP …` bookings with `AMZ…` booking numbers, Century
  (`cds-net.com`) alerts and `mDDP` ISF notices; stock shipments come through
  WWL's own arrival notices.
* Loader: `--shipment-type stock|amazon_di` sets it; left null when unknown.

## Reference data for freight invoices

### Rate sheets — `finance.freight_rate_sheets` (to build)

WWL publishes rate sheets on the **1st and 15th** of each month, separately
for East Coast and West Coast (agreed with WWL on 2026-10-16). The applicable
sheet is the one in force on the **container's origin in-gate date**, not the
booking or sailing date (WWL, 2026-10-14, HBL TAO26070091: in-gated 8/5 → the
8/1 sheet, $12,020; TAO26080010 in-gated 8/20 → the 8/15 sheet plus a carrier
special rate, $10,460).

* Grain: sheet date, coast, carrier, origin port, destination, equipment
  (20/40/40HC), service, rate, special-rate flag.
* The Apps Script saves the sheet PDFs to `Rate-Sheets/`; parsing them is a
  separate loader once the layout is known.

### FCL vs LCL rules

Per the approver, the two are built differently, so each gets its own rule set:

* **FCL** — per container: rate sheet lookup by carrier / lane / equipment /
  in-gate date, plus listed surcharges. Per diem and demurrage come on a
  separate, later invoice and are checked against free time and gate-out /
  return dates.
* **LCL** — per volume/weight (W/M) with minimums, plus CFS and destination
  handling charges. The rule set needs sample LCL invoices and the LCL part of
  the rate sheet before it can be written.

## Backup files the validator needs, per customs entry

Every check in `tools/customs_entries_load.py` today reads only the 7501
itself. That is enough to prove the form is internally consistent, not that it
is correct. Each row below is a fact the validator cannot get from the 7501,
and the check it unlocks.

| # | Backup | Fields used | Unlocks | Source | Have it? |
|---|---|---|---|---|---|
| 1 | **7501 entry summary**, all pages | everything | levels 1–2 (built) | broker → Bill.com / Administrator@ | 1 of ~24 recent |
| 2 | **Factory commercial invoice + packing list** for every invoice no. on the 7501 | invoice no., PO, SKU, qty, unit price, total, currency, **incoterm**, freight/insurance lines, manufacturer | entered value vs real price; carton count vs 7501; manufacturer ID | sourcing → broker ("shipping documents") | no |
| 3 | **Shipment type** per entry: stock or Amazon DI, and for DI the Amazon PO | type, Amazon PO | which valuation rule applies to #2 | booking (AMZ… / MPP bookings) — confirm per entry | no |
| 4 | **Tariff rates in force**, per HTS + origin + program, with effective dates and which date governs | rate, program, from/to | rate on each line was the right one on that date; chapter 99 lines missing/extra | USITC HTS revisions; broker tariff updates | no |
| 5 | **Approved HTS classification per SKU** | SKU → HTS | same product entered under different codes | sourcing / broker | no |
| 6 | **Ocean freight invoice** for the container | freight charged, B/L, container | 7501's "(-) International Freight" deduction = freight actually paid | WWL freight invoice (LAX/EWR…) | no |
| 7 | **Customs release notice** | entry no. ↔ B/L ↔ container | ties #1–#6 and #8 to one shipment | WWL release emails | yes, by email |
| 8 | **Duty bill as entered for payment** | entry no., amount, due/paid date | billed = box 44; no duty paid twice (e.g. CLA261000320 filed under the CBP vendor) | Bill.com | approval emails only |
| 9 | **Broker's own invoice** (brokerage/clearance fees) | fees, entry ref | brokerage billed once, at contract rate | broker CLA… invoice | no |
| 10 | **MPF min/max for the fiscal year** | min, max | MPF check | CBP, each October | FY25–26 only; **FY27 missing** (entries from 2026-10-01) |

Decisions that are not files, and only the approver can make: the valuation
rule for stock vs Amazon DI (#3), and the tolerance at which a finding stops
an approval vs only notes it.

Example of why #2 matters: entry 8NW-1591298-8 declares $11,745 but its lines
total $2,575. The 7501 alone cannot say which is right; the factory invoice
(`001/KMLT10036`, $17,497.05 per the continuation sheet) and the shipment type
can.

## Open questions for the approver

1. Amazon DI vs stock: what is the valuation basis for each, and how should the
   entered value relate to the factory invoice in each case?
2. Which tariff programs and HTS numbers matter most — start with the chapter
   99 lines already on our entries?
3. Who issues filer code `NYS` entries (13 of 24 CBP bills, Oct 4–20, ~$112K),
   and where do their 7501s go?
4. Sample LCL invoices and the LCL rate sheet format.
5. Per diem / demurrage: where do free-time terms live (contract, rate sheet)?

## Build order

1. Apps Script export from Administrator@ → Drive + manifest sheet (backfill from 2026-06-01).
2. Load the backfilled 7501s into `customs_entries` (levels 1–2 already work).
3. `customs_releases` from the release emails in the manifest (entry ↔ BL ↔ container).
4. `tariff_rates` for the HTS numbers we import → `rate_in_force` check.
5. Factory invoices + shipment type rules → entered-value check.
6. Rate sheets + FCL rules → ocean freight check; LCL once samples exist.

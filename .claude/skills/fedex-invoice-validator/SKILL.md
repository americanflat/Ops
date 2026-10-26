---
name: fedex-invoice-validator
description: >
  Validates a FedEx invoice CSV against Stedi 945 shipping data in BigQuery for
  Americanflat. Use when the user uploads a FedEx invoice export
  (FedEx_invoice_*.CSV) or says "validate the FedEx invoice", "check FedEx
  against Stedi / the 945s", "FedEx marketplace breakdown", or "which FedEx
  orders have no marketplace". Matches every tracking ID to a 945 ship
  confirmation, rolls net charges up by marketplace, and lists the orders that
  have no marketplace identifier.
---

# FedEx Invoice Validator

Checks every line of a FedEx invoice against the Stedi 945 ship confirmations
and says which marketplace each charge belongs to.

## Run it

```bash
python3 tools/fedex_invoice_validate.py --out-dir <scratchpad>/fedex <invoice.csv> [more.csv ...]
# once the user supplies 940s for the orders with no 945:
python3 tools/fedex_invoice_validate.py --edi-940 <940 json files or dir> --out-dir <scratchpad>/fedex <invoice.csv>
```

Read-only (SELECT queries only) and needs only the `bq` CLI, so it works with
the session's read-only BigQuery credential. Several invoice files can be passed
at once. Do not hand-roll the matching query; the script holds the rules below.

## How it matches

Source: `americanflat.finance.shipment_reconciliation` — the Stedi 945 lines
(`warehouse.shipments_945`) with each order resolved to a channel through
`warehouse.channel_keys`.

1. FedEx `Express or Ground Tracking ID` = 945 `cartonTracking`
2. else FedEx `Original Customer Reference` (the SSCC) = 945 `ssccBarcode`
3. else FedEx Ref#2 / Ref#3 = 945 `orderNumber` / `warehouseReference`
   (`matched_on = order_ref`). The order has a 945, but this billed label is
   not one of its cartons: e.g. order 28542's 945 lists 3 cartons and FedEx
   billed 4. The line takes the order's marketplace, and is also listed under
   "Billed labels not on the order's 945" for review with the warehouse.

**Reships.** The warehouse cannot process the same order number twice, so a
reship is sent as the original marketplace order number followed by `R`, `R`
plus a number, or `*` plus a number (`27402R2`, `4791738745R`). When the 945
order number or a FedEx reference has that shape and the part before the
suffix is a known marketplace order, the line is resolved to that marketplace
as a `reship`, with `original_order` naming the original order.

**Replacements.** Order numbers starting `RPL` (945 `RPL2651600F30F25`, FedEx
reference `RPL-26516-00F30F25`) are replacements for Shopify orders. They
resolve to SHIPSTATION (Shopify) as a `replacement`, with `original_order` set
to the number between the dashes when a FedEx reference carries it.

**Ship-to name.** If an order still has no marketplace but its 945 ship-to name
names one, it resolves as `ship_to`. The `CG…` orders (e.g. `CG100126SCWF1`)
ship to "Wayfair Store …", "Wayfair Warehouse …" or "CastleGate Warehouse …",
so they resolve to Wayfair. Add new names to `SHIP_TO_CHANNELS` in the script.

**No 945 → check the 940.** If a line has no 945 at all, the marketplace comes
from the order's 940 (warehouse shipping order). 940s are **not in BigQuery**,
so ask the user for them (Stedi JSON, one file per order or a folder) and rerun
with `--edi-940 <files or dir>`. The script matches the 940's W05 depositor/PO
number to the FedEx references and reads the marketplace from `N9*DP`
(department, e.g. "Target"), else `N9*IA`. These lines resolve as `940`, and
are also a finding: the order went to the warehouse but its shipment was never
confirmed with a 945.

Reships, replacements, ship-to and 940 matches count toward their marketplace;
they are not orders without a marketplace.

Each line ends up `resolved` (one channel), `reship` / `replacement`
(resolved through the original order), `ship_to` (resolved from the 945
ship-to name), `940` (no 945; marketplace from the 940), `unresolved` (in the 945s but the order number matches no
marketplace), `ambiguous` (matches several), or `not_in_945` (no ship
confirmation at all).

For anything still without a marketplace, the script looks FedEx Ref#2 / Ref#3
up in `channel_keys` after stripping warehouse decorations (`AME*`/`AMS*`,
`-A`, `WHS-<n>-<po>`) and prints a **suggested** channel.
Present it as a suggestion, not a resolution.

## What to report back

1. Headline: lines, invoices, total net charge (must equal the sum of the
   invoices' `Net Charge Amount`), and the match breakdown.
2. The marketplace table (lines, orders, net charge, avg per line), then the
   reships and replacements with the original order each one belongs to.
3. Billed labels not on the order's 945: the warehouse shipped more cartons
   than it confirmed, or relabelled one. Worth raising with the warehouse.
4. **Orders with no marketplace identifier** — always list them all, grouped:
   - has a suggested channel → usually a shipment missing from the 945 data.
   - internal (SAMPLE*, ShootSample*, PODTEST*, UGC-*) → company cost, no marketplace.
   - `not_in_945` with no references → shipped outside the warehouse (Kent's
     overnights, return labels); flag for review.
5. Send `fedex_orders_without_marketplace.csv` (and the full
   `fedex_validation_lines.csv` if asked) to the user.

Channel codes: TARG Target, SHIPSTATION Shopify/ShipStation, WAYF Wayfair,
MACY Macy's, WALC Walmart, KOHLS Kohl's, AMZC Amazon DF, AMZVC Amazon VC.

## When things look off

- Many `not_in_945`: the 945 load may lag the invoice; check
  `MAX(shipDate)` in `warehouse.shipments_945` against the invoice ship dates.
- A recurring order-number pattern stays `unresolved` with a suggestion every
  week: fix it at the source by adding the normalization to
  `warehouse.channel_keys` / `finance.shipment_reconciliation`, not here.

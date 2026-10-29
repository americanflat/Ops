# Actionable Complaints dashboard

Tells Product development, Ops and Sales which customer complaints need action,
and links each flag to the Zendesk tickets behind it.

* **Dashboard:** https://claude.ai/artifact/L4PXRYgnNX2g4DP6tHFuJ5
* **Builder:** [`tools/cx_complaint_signals.py`](../tools/cx_complaint_signals.py)
* **Refresh:** a Claude scheduled task runs it daily and uploads the results
  (see "Daily refresh" below).

## Where the data comes from

All BigQuery, so a cloud session needs no API keys:

| Table | Used for |
|---|---|
| `zendesk.tickets`, `zendesk.customer_messages` | the complaint and what the customer said (agent replies excluded) |
| `shopify.orders`, `shopify.order_line_items` | the SKUs in the order on the ticket, and units sold per SKU |
| `warehouse.shipments_945` | the warehouse and carrier that shipped the order |

The order number is taken from the ticket's order field, subject or text
(5 digits, 2xxxx or 3xxxx). The old `shipstation.shipments_raw` table stopped
in 2023, so carriers come from the warehouse feed.

## What gets flagged

Classification is keyword-based on the customer's own words, so it is
deterministic and every flag can be checked against its tickets. Agents often
file damage claims under *product_detail_inquiry*, so the words decide, but a
complaint also needs a complaint category or an order number, so newsletters
and spam that mention "damage" don't count.

| Team | Signal | Flag when (recent = last 30 days) |
|---|---|---|
| Product | Damage claims by SKU | 3+ recent claims, or 5%+ of units sold with 4+ claims, counting only orders where that SKU was the only item. Labelled "Likely product defect" when most claims describe build problems (corners, gaps, warping, missing hardware), otherwise "Packaging / transit damage". |
| Product | Questions the listing doesn't answer | Same topic (depth, outer size, glass/UV, hanging, mat sizing, material, color, fit) on one product line 4+ times in the window, 1+ recent |
| Ops | Wrong or missing items by SKU | 2+ claims where that SKU was the only item, 1+ recent |
| Ops | Damage by warehouse and carrier | damage claims per order shipped at 1.25× the average across routes, with 5+ claims |
| Sales | Bulk and wholesale leads | every recent ticket mentioning wholesale, resale, a quote, PO, tax-exempt buying or 25+ units (vendor pitches excluded) |
| Sales | Out-of-stock demand | 2+ recent "back in stock" asks on one product line |
| Sales | Sizes asked about | sizes named in "do you make / sell / have" questions (no flag; for range planning) |

Thresholds are constants at the top of the script. Ticket excerpts have email
addresses, links and phone numbers removed.

## Running it

```bash
python3 tools/cx_complaint_signals.py --out /tmp/cx/report.json --datasets /tmp/cx/ds
```

`--datasets` writes one JSON file per dashboard dataset: `summary`,
`sku_damage`, `questions`, `picks`, `routes`, `leads`, `oos`, `sizes`,
`evidence`.

## Daily refresh

The scheduled task does this each morning:

1. Run the command above.
2. For each file in `/tmp/cx/ds`, upload it to the dashboard as an asset
   (Artifact tool, `asset: true`, the dashboard URL).
3. `ArtifactData get` `datasets/<name>`, then `update` it (with `if_version`)
   to the new `source.url` and `source.name`, and
   `updated: {at: <now>, by: "Claude scheduled refresh"}`.
4. Delete the previous asset for each dataset (its old `source.url` id), so
   old customer text doesn't pile up.

Don't change `files/index.html` in the refresh. Page changes are made in a
normal session.

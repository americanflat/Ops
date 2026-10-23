# CX Scorecard

A customer-experience dashboard over the Zendesk sync in BigQuery, with metrics
you pick from a catalog. It lives at
https://claude.ai/artifact/5jVfSs29zeRkJ7uRzvxdx1 (private until shared).

The four queries behind it are in `sql/`:

| File | Feeds |
|---|---|
| `cx_scorecard_metrics.sql` | Every metric card and the promises table. One row per (period, metric): each Monday week since 2025-12-01, plus `last_28d` and `prior_28d`. |
| `cx_scorecard_sku.sql` | Damage, missing and wrong-item tickets per SKU, for tickets that name a Shopify order. |
| `cx_scorecard_warehouse.sql` | Damage tickets per 100 Shopify orders, by shipping warehouse. |
| `cx_scorecard_csat.sql` | Satisfaction surveys and ratings per month. |

The dashboard holds a saved copy of each result. To make it live, sign in the
Google Cloud BigQuery connector in claude.ai and point each dataset at its query.

## Promises measured

Quoted from the store's published policies (Shopify shop policies, read 2026-10-23).

| Promise | Metric | Target |
|---|---|---|
| "We aim to respond to all inquiries within 1–2 business days." | `reply_2bd_pct` | 95% |
| "Orders are typically processed within 1–2 business days." | `ship_2bd_pct` | 95% |
| Damage claims need "photos of both the frame and the box." | `damage_photo_pct` | 90% |

Not measurable yet: delivery in 5–7 business days (no carrier delivery dates),
replacement shipped after photos (replacements aren't linked to tickets), and
refunds within 7–10 business days of receipt (no return-received date).

## Definitions worth knowing

- **Business days** skip weekends but not U.S. holidays. A ticket or order on
  day D counts days D+1 through the reply or ship date.
- **Replied within 2 business days** counts tickets still unanswered after 2
  business days as misses, so a backlog can't hide.
- **Ship date** is the first warehouse ship advice in
  `finance.shipment_reconciliation`. It covers Shopify orders from January 2026.
- **Box condition** is read from the customer's own words with regular
  expressions. About 1 in 8 damage tickets say what the box looked like; the
  rest are "not stated". A required Zendesk field would replace this.
- **SKU attribution** joins the ticket's order number to Shopify line items. A
  ticket counts against every SKU on its order. Amazon order IDs on tickets
  don't match any table in BigQuery, so Amazon damage has no SKU.

## Benchmarks used for targets

- First email reply within 12 hours is "good" (Zendesk guidance).
- Ecommerce CSAT typically runs 78–82%.
- First-contact resolution of 70–80% is good for ecommerce email and chat.

These are vendor-published figures, used as reference points rather than
industry standards.

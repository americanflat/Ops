# CX Scorecard

A customer-experience dashboard over the Zendesk sync in BigQuery, with metrics
you pick from a catalog. It lives at
https://claude.ai/artifact/5jVfSs29zeRkJ7uRzvxdx1 (private until shared).

The dashboard has a date range. It holds one row per ticket, per order and per
day, and works out every metric for whatever range is picked, compared with the
same number of days just before it. The five queries behind it are in `sql/`:

| File | Rows |
|---|---|
| `cx_scorecard_tickets.sql` | One per ticket since 2025-12-01: arrival day, first reply, business days to reply, solved, survey, issue type, box condition, photo, Shopify order. No IDs, names or text. |
| `cx_scorecard_orders.sql` | One per Shopify order: business days to ship, warehouse, whether it got a damage ticket. |
| `cx_scorecard_daily.sql` | One per day: agent replies written, open queue at day end. |
| `cx_scorecard_sku_issues.sql` | One per issue ticket and SKU on its Shopify order. |
| `cx_scorecard_sku_units.sql` | Shopify units sold per SKU per week. |

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

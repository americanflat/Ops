-- Damage tickets per 100 Shopify orders, by the warehouse that shipped the order, since Jan 1, 2026.
WITH ship AS (
  SELECT orderNumber, ANY_VALUE(warehouse) AS warehouse, COUNT(DISTINCT warehouse) AS n_wh
  FROM `americanflat.finance.shipment_reconciliation` GROUP BY 1),
o AS (
  SELECT REGEXP_EXTRACT(name, r"(\d+)") AS order_no
  FROM `americanflat.shopify.orders` WHERE created_at >= "2026-01-01"),
d AS (
  SELECT DISTINCT REGEXP_EXTRACT(order_reference, r"^\s*#?(\d{4,6})\s*$") AS order_no
  FROM `americanflat.zendesk.tickets`
  WHERE category = "order___damage_item" AND NOT COALESCE(is_deleted, FALSE) AND status != "deleted")
SELECT CASE WHEN ship.n_wh > 1 THEN "Split across warehouses" WHEN ship.warehouse = "NJ" THEN "Edison, NJ"
            WHEN ship.warehouse = "SC" THEN "Hardeeville, SC" WHEN ship.warehouse = "FON" THEN "Fontana, CA" ELSE ship.warehouse END AS warehouse,
  COUNT(*) AS orders_shipped,
  COUNTIF(d.order_no IS NOT NULL) AS orders_with_damage_ticket,
  ROUND(SAFE_DIVIDE(COUNTIF(d.order_no IS NOT NULL), COUNT(*)) * 100, 2) AS damage_per_100_orders
FROM o JOIN ship ON ship.orderNumber = o.order_no LEFT JOIN d ON d.order_no = o.order_no
GROUP BY 1 ORDER BY orders_shipped DESC

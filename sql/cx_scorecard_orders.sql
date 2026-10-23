-- One row per Shopify order since 2025-12-01: business days to first warehouse ship date, warehouse, and whether it got a damage ticket.
WITH ship AS (
  SELECT orderNumber, MIN(shipDate) AS sd, ANY_VALUE(warehouse) AS wh, COUNT(DISTINCT warehouse) AS n_wh
  FROM `americanflat.finance.shipment_reconciliation` GROUP BY 1),
o AS (
  SELECT REGEXP_EXTRACT(name, r"(\d+)") AS order_no, DATE(created_at, "America/New_York") AS d
  FROM `americanflat.shopify.orders` WHERE created_at >= "2025-12-01"),
dmg AS (
  SELECT DISTINCT REGEXP_EXTRACT(order_reference, r"^\s*#?(\d{4,6})\s*$") AS order_no
  FROM `americanflat.zendesk.tickets`
  WHERE category = "order___damage_item" AND NOT COALESCE(is_deleted, FALSE) AND status != "deleted")
SELECT FORMAT_DATE("%Y-%m-%d", o.d) AS day,
  IF(ship.sd IS NULL OR o.d < "2026-01-01", NULL,
    (SELECT COUNT(*) FROM UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(o.d, INTERVAL 1 DAY), ship.sd)) x WHERE EXTRACT(DAYOFWEEK FROM x) NOT IN (1, 7))) AS ship_bd,
  CASE WHEN ship.sd IS NULL OR o.d < "2026-01-01" THEN NULL WHEN ship.n_wh > 1 THEN "Split across warehouses" WHEN ship.wh = "NJ" THEN "Edison, NJ"
       WHEN ship.wh = "SC" THEN "Hardeeville, SC" WHEN ship.wh = "FON" THEN "Fontana, CA" ELSE ship.wh END AS warehouse,
  dmg.order_no IS NOT NULL AS damage_ticket
FROM o LEFT JOIN ship ON ship.orderNumber = o.order_no LEFT JOIN dmg ON dmg.order_no = o.order_no
ORDER BY day

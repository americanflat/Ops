-- Damaged, missing and wrong-item tickets per SKU, for tickets with a Shopify order number, since Dec 1, 2025.
-- Each ticket counts against every SKU on its order. Units sold come from Shopify order lines.
WITH today AS (SELECT CURRENT_DATE("America/New_York") AS t),
tk AS (
  SELECT t.ticket_id, DATE(t.created_at, "America/New_York") AS d, t.category,
    REGEXP_EXTRACT(t.order_reference, r"^\s*#?(\d{4,6})\s*$") AS order_no
  FROM `americanflat.zendesk.tickets` t
  WHERE t.category IN ("order___damage_item", "order___missing_item", "orders__wrong_item")
    AND NOT COALESCE(t.is_deleted, FALSE) AND t.status != "deleted" AND t.created_at >= "2025-12-01"),
txt AS (
  SELECT tk.ticket_id, LOWER(STRING_AGG(COALESCE(m.plain_body, m.body), " ")) AS x
  FROM tk LEFT JOIN `americanflat.zendesk.customer_messages` m USING (ticket_id)
  WHERE tk.category = "order___damage_item" GROUP BY 1),
box AS (
  SELECT ticket_id,
    CASE
      WHEN REGEXP_CONTAINS(x, r"(box|packaging|package|carton)[^.!?]{0,30}(fine|perfect|intact|undamaged|good (shape|condition)|no damage|not damaged|looked (ok|okay|fine|good)|no (visible )?sign)") THEN "fine"
      WHEN REGEXP_CONTAINS(x, r"(box|packaging|carton|package)[^.!?]{0,25}\b(damaged|crushed|dented|torn|ripped|smashed|beat up|beaten up|punctured|hole|wet|caved)\b")
        OR REGEXP_CONTAINS(x, r"\b(damaged|crushed|dented|torn|ripped|smashed|beat up|caved in)\b[^.!?]{0,10}(box|packaging|carton|package)") THEN "damaged"
      ELSE "not_stated" END AS box,
    REGEXP_CONTAINS(x, r"\b(glass|plexi\w*|acrylic)\b[^.!?]{0,40}\b(broke|broken|shatter\w*|crack\w*)|\b(broke|broken|shatter\w*|crack\w*)\b[^.!?]{0,40}\b(glass|plexi\w*|acrylic)\b") AS glass
  FROM txt),
o AS (SELECT order_id, REGEXP_EXTRACT(name, r"(\d+)") AS order_no FROM `americanflat.shopify.orders`),
li AS (SELECT order_id, sku, ANY_VALUE(title) AS title FROM `americanflat.shopify.order_line_items` WHERE COALESCE(sku, "") != "" GROUP BY 1, 2),
hits AS (
  SELECT tk.*, li.sku, li.title, box.box, box.glass
  FROM tk JOIN o USING (order_no) JOIN li USING (order_id) LEFT JOIN box USING (ticket_id)),
sold AS (
  SELECT sku,
    SUM(IF(DATE(order_created_at, "America/New_York") >= DATE_SUB((SELECT t FROM today), INTERVAL 90 DAY), quantity, 0)) AS units_90d,
    SUM(IF(DATE(order_created_at, "America/New_York") BETWEEN DATE_SUB((SELECT t FROM today), INTERVAL 180 DAY) AND DATE_SUB((SELECT t FROM today), INTERVAL 91 DAY), quantity, 0)) AS units_prior_90d,
    SUM(IF(order_created_at >= "2025-11-15", quantity, 0)) AS units_since_dec
  FROM `americanflat.shopify.order_line_items` GROUP BY 1)
SELECT h.sku, ANY_VALUE(h.title) AS product,
  COUNT(DISTINCT IF(h.category = "order___damage_item", h.ticket_id, NULL)) AS damaged,
  COUNT(DISTINCT IF(h.category = "order___damage_item" AND h.box = "damaged", h.ticket_id, NULL)) AS box_damaged,
  COUNT(DISTINCT IF(h.category = "order___damage_item" AND h.box = "fine", h.ticket_id, NULL)) AS box_fine,
  COUNT(DISTINCT IF(h.category = "order___damage_item" AND h.glass, h.ticket_id, NULL)) AS glass_broken,
  COUNT(DISTINCT IF(h.category = "order___missing_item", h.ticket_id, NULL)) AS missing,
  COUNT(DISTINCT IF(h.category = "orders__wrong_item", h.ticket_id, NULL)) AS wrong_item,
  COUNT(DISTINCT IF(h.category = "order___damage_item" AND h.d >= DATE_SUB((SELECT t FROM today), INTERVAL 90 DAY), h.ticket_id, NULL)) AS damaged_90d,
  COUNT(DISTINCT IF(h.category = "order___damage_item" AND h.d BETWEEN DATE_SUB((SELECT t FROM today), INTERVAL 180 DAY) AND DATE_SUB((SELECT t FROM today), INTERVAL 91 DAY), h.ticket_id, NULL)) AS damaged_prior_90d,
  ANY_VALUE(s.units_since_dec) AS units_sold,
  ANY_VALUE(s.units_90d) AS units_90d,
  ANY_VALUE(s.units_prior_90d) AS units_prior_90d,
  ROUND(SAFE_DIVIDE(COUNT(DISTINCT IF(h.category = "order___damage_item", h.ticket_id, NULL)), ANY_VALUE(s.units_since_dec)) * 100, 2) AS damage_per_100_units
FROM hits h LEFT JOIN sold s USING (sku)
GROUP BY h.sku
HAVING damaged + missing + wrong_item > 0
ORDER BY damaged DESC, missing + wrong_item DESC

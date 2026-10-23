-- One row per (issue ticket, SKU on its Shopify order) since 2025-12-01, plus box condition and broken-glass mentions.
WITH tk AS (
  SELECT t.ticket_id, DATE(t.created_at, "America/New_York") AS d, t.category,
    REGEXP_EXTRACT(t.order_reference, r"^\s*#?(\d{4,6})\s*$") AS order_no
  FROM `americanflat.zendesk.tickets` t
  WHERE t.category IN ("order___damage_item", "order___missing_item", "orders__wrong_item")
    AND NOT COALESCE(t.is_deleted, FALSE) AND t.status != "deleted" AND t.created_at >= "2025-12-01"),
txt AS (
  SELECT tk.ticket_id, LOWER(STRING_AGG(COALESCE(m.plain_body, m.body), " ")) AS x
  FROM tk LEFT JOIN `americanflat.zendesk.customer_messages` m USING (ticket_id)
  WHERE tk.category = "order___damage_item" GROUP BY 1),
o AS (SELECT order_id, REGEXP_EXTRACT(name, r"(\d+)") AS order_no FROM `americanflat.shopify.orders`),
li AS (SELECT DISTINCT order_id, sku FROM `americanflat.shopify.order_line_items` WHERE COALESCE(sku, "") != "")
SELECT FORMAT_DATE("%Y-%m-%d", tk.d) AS day, li.sku,
  CASE tk.category WHEN "order___damage_item" THEN "damage" WHEN "order___missing_item" THEN "missing" ELSE "wrong" END AS issue,
  IF(tk.category = "order___damage_item",
    CASE
      WHEN REGEXP_CONTAINS(txt.x, r"(box|packaging|package|carton)[^.!?]{0,30}(fine|perfect|intact|undamaged|good (shape|condition)|no damage|not damaged|looked (ok|okay|fine|good)|no (visible )?sign)") THEN "fine"
      WHEN REGEXP_CONTAINS(txt.x, r"(box|packaging|carton|package)[^.!?]{0,25}\b(damaged|crushed|dented|torn|ripped|smashed|beat up|beaten up|punctured|hole|wet|caved)\b")
        OR REGEXP_CONTAINS(txt.x, r"\b(damaged|crushed|dented|torn|ripped|smashed|beat up|caved in)\b[^.!?]{0,10}(box|packaging|carton|package)") THEN "damaged"
      ELSE "not_stated" END, NULL) AS box,
  IF(tk.category = "order___damage_item",
    REGEXP_CONTAINS(txt.x, r"\b(glass|plexi\w*|acrylic)\b[^.!?]{0,40}\b(broke|broken|shatter\w*|crack\w*)|\b(broke|broken|shatter\w*|crack\w*)\b[^.!?]{0,40}\b(glass|plexi\w*|acrylic)\b"), NULL) AS glass
FROM tk JOIN o USING (order_no) JOIN li USING (order_id) LEFT JOIN txt USING (ticket_id)
ORDER BY day

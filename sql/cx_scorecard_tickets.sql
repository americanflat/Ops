-- One row per ticket since 2025-12-01 (deleted tickets left out). No ticket IDs, names or text.
WITH today AS (SELECT CURRENT_DATE("America/New_York") AS t),
tk AS (
  SELECT ticket_id, created_at, DATE(created_at, "America/New_York") AS d, COALESCE(is_junk, FALSE) AS junk,
    status, category, satisfaction_score, order_reference
  FROM `americanflat.zendesk.tickets`
  WHERE NOT COALESCE(is_deleted, FALSE) AND status != "deleted" AND created_at >= "2025-12-01"),
agent_c AS (
  SELECT c.ticket_id, c.created_at FROM `americanflat.zendesk.ticket_comments` c
  JOIN `americanflat.zendesk.users` u ON u.user_id = c.author_id
  WHERE c.public AND u.role IN ("agent", "admin")),
fr AS (
  SELECT tk.ticket_id, MIN(a.created_at) AS first_reply_at, COUNT(a.ticket_id) AS agent_replies
  FROM tk LEFT JOIN agent_c a ON a.ticket_id = tk.ticket_id AND a.created_at >= tk.created_at GROUP BY 1),
photo AS (SELECT DISTINCT ticket_id FROM `americanflat.zendesk.ticket_comments` WHERE has_attachments),
txt AS (
  SELECT tk.ticket_id, LOWER(STRING_AGG(COALESCE(m.plain_body, m.body), " ")) AS x
  FROM tk LEFT JOIN `americanflat.zendesk.customer_messages` m USING (ticket_id)
  WHERE tk.category = "order___damage_item" GROUP BY 1),
shop AS (SELECT DISTINCT REGEXP_EXTRACT(name, r"(\d+)") AS order_no FROM `americanflat.shopify.orders`)
SELECT
  FORMAT_DATE("%Y-%m-%d", tk.d) AS day,
  tk.junk,
  ROUND(TIMESTAMP_DIFF(fr.first_reply_at, tk.created_at, MINUTE) / 60, 2) AS frt_hrs,
  (SELECT COUNT(*) FROM UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(tk.d, INTERVAL 1 DAY),
      COALESCE(DATE(fr.first_reply_at, "America/New_York"), (SELECT t FROM today)))) x
   WHERE EXTRACT(DAYOFWEEK FROM x) NOT IN (1, 7)) AS reply_bd,
  fr.agent_replies,
  tk.status IN ("solved", "closed") AS solved,
  IF(tk.satisfaction_score IN ("good", "bad", "offered"), tk.satisfaction_score, NULL) AS csat,
  CASE tk.category WHEN "order___damage_item" THEN "damage" WHEN "order___missing_item" THEN "missing" WHEN "orders__wrong_item" THEN "wrong" END AS issue,
  IF(tk.category = "order___damage_item",
    CASE
      WHEN REGEXP_CONTAINS(txt.x, r"(box|packaging|package|carton)[^.!?]{0,30}(fine|perfect|intact|undamaged|good (shape|condition)|no damage|not damaged|looked (ok|okay|fine|good)|no (visible )?sign)") THEN "fine"
      WHEN REGEXP_CONTAINS(txt.x, r"(box|packaging|carton|package)[^.!?]{0,25}\b(damaged|crushed|dented|torn|ripped|smashed|beat up|beaten up|punctured|hole|wet|caved)\b")
        OR REGEXP_CONTAINS(txt.x, r"\b(damaged|crushed|dented|torn|ripped|smashed|beat up|caved in)\b[^.!?]{0,10}(box|packaging|carton|package)") THEN "damaged"
      ELSE "not_stated" END, NULL) AS box,
  IF(tk.category = "order___damage_item", photo.ticket_id IS NOT NULL, NULL) AS photo,
  IF(tk.category = "order___damage_item", shop.order_no IS NOT NULL, NULL) AS shopify_order
FROM tk
LEFT JOIN fr USING (ticket_id)
LEFT JOIN photo USING (ticket_id)
LEFT JOIN txt USING (ticket_id)
LEFT JOIN shop ON shop.order_no = REGEXP_EXTRACT(tk.order_reference, r"^\s*#?(\d{4,6})\s*$")
ORDER BY day

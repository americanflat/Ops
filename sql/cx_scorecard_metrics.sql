-- One row per (period, metric). Periods: every Monday-start week since 2025-12-01, plus the last 28 days and the 28 days before.
WITH today AS (SELECT CURRENT_DATE("America/New_York") AS t),
buckets AS (
  SELECT "week" AS kind, ws AS start_d, LEAST(DATE_ADD(ws, INTERVAL 6 DAY), (SELECT t FROM today)) AS end_d
  FROM UNNEST(GENERATE_DATE_ARRAY(DATE "2025-12-01", DATE_TRUNC((SELECT t FROM today), WEEK(MONDAY)), INTERVAL 7 DAY)) ws
  UNION ALL SELECT "last_28d", DATE_SUB(t, INTERVAL 28 DAY), DATE_SUB(t, INTERVAL 1 DAY) FROM today
  UNION ALL SELECT "prior_28d", DATE_SUB(t, INTERVAL 56 DAY), DATE_SUB(t, INTERVAL 29 DAY) FROM today),
tk AS (
  SELECT ticket_id, created_at, DATE(created_at, "America/New_York") AS d, COALESCE(is_junk, FALSE) AS junk,
    status, category, satisfaction_score, order_reference
  FROM `americanflat.zendesk.tickets`
  WHERE NOT COALESCE(is_deleted, FALSE) AND status != "deleted" AND created_at >= "2025-12-01"),
agent_c AS (
  SELECT c.ticket_id, c.created_at, c.has_attachments
  FROM `americanflat.zendesk.ticket_comments` c JOIN `americanflat.zendesk.users` u ON u.user_id = c.author_id
  WHERE c.public AND u.role IN ("agent", "admin")),
fr AS (
  SELECT tk.ticket_id, tk.d, tk.status, MIN(a.created_at) AS first_reply_at, COUNT(a.ticket_id) AS agent_replies,
    TIMESTAMP_DIFF(MIN(a.created_at), ANY_VALUE(tk.created_at), MINUTE) / 60 AS frt_hrs
  FROM tk LEFT JOIN agent_c a ON a.ticket_id = tk.ticket_id AND a.created_at >= tk.created_at
  WHERE NOT tk.junk GROUP BY 1, 2, 3),
fr_bd AS (
  SELECT fr.*,
    (SELECT COUNT(*) FROM UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(d, INTERVAL 1 DAY),
        COALESCE(DATE(first_reply_at, "America/New_York"), (SELECT t FROM today)))) x
     WHERE EXTRACT(DAYOFWEEK FROM x) NOT IN (1, 7)) AS bd
  FROM fr),
dmg AS (
  SELECT tk.ticket_id, tk.d,
    LOWER(STRING_AGG(COALESCE(m.plain_body, m.body), " ")) AS txt,
    LOGICAL_OR(COALESCE(c.has_attachments, FALSE)) AS photo
  FROM tk
  LEFT JOIN `americanflat.zendesk.customer_messages` m USING (ticket_id)
  LEFT JOIN `americanflat.zendesk.ticket_comments` c ON c.ticket_id = tk.ticket_id AND c.has_attachments
  WHERE tk.category = "order___damage_item" GROUP BY 1, 2),
dmg_c AS (
  SELECT ticket_id, d, photo,
    CASE
      WHEN REGEXP_CONTAINS(txt, r"(box|packaging|package|carton)[^.!?]{0,30}(fine|perfect|intact|undamaged|good (shape|condition)|no damage|not damaged|looked (ok|okay|fine|good)|no (visible )?sign)") THEN "fine"
      WHEN REGEXP_CONTAINS(txt, r"(box|packaging|carton|package)[^.!?]{0,25}\b(damaged|crushed|dented|torn|ripped|smashed|beat up|beaten up|punctured|hole|wet|caved)\b")
        OR REGEXP_CONTAINS(txt, r"\b(damaged|crushed|dented|torn|ripped|smashed|beat up|caved in)\b[^.!?]{0,10}(box|packaging|carton|package)") THEN "damaged"
      ELSE "not_stated" END AS box
  FROM dmg),
so AS (
  SELECT REGEXP_EXTRACT(o.name, r"(\d+)") AS order_no, DATE(o.created_at, "America/New_York") AS d, s.sd
  FROM `americanflat.shopify.orders` o
  LEFT JOIN (SELECT orderNumber, MIN(shipDate) sd FROM `americanflat.finance.shipment_reconciliation` GROUP BY 1) s
    ON s.orderNumber = REGEXP_EXTRACT(o.name, r"(\d+)")
  WHERE o.created_at >= "2025-12-01"),
so_bd AS (
  SELECT so.*, (SELECT COUNT(*) FROM UNNEST(GENERATE_DATE_ARRAY(DATE_ADD(d, INTERVAL 1 DAY), sd)) x
                WHERE EXTRACT(DAYOFWEEK FROM x) NOT IN (1, 7)) AS ship_bd
  FROM so WHERE sd IS NOT NULL AND d >= "2026-01-01"),
shop_dmg AS (
  SELECT tk.d FROM tk JOIN (SELECT DISTINCT order_no FROM so) s
    ON s.order_no = REGEXP_EXTRACT(tk.order_reference, r"^\s*#?(\d{4,6})\s*$")
  WHERE tk.category = "order___damage_item"),
rep AS (SELECT DATE(created_at, "America/New_York") AS d, COUNT(*) n FROM agent_c WHERE created_at >= "2025-12-01" GROUP BY 1),
hist AS (SELECT ticket_id, status, is_junk, is_deleted, DATE(ingested_at, "America/New_York") AS d, ingested_at FROM `americanflat.zendesk.tickets_history`),
queue AS (
  SELECT b.kind, b.start_d, COUNTIF(st.status IN ("new", "open", "pending") AND NOT COALESCE(st.is_junk, FALSE) AND NOT COALESCE(st.is_deleted, FALSE)) AS open_queue
  FROM (SELECT b.kind, b.start_d, h.ticket_id, ARRAY_AGG(STRUCT(h.status, h.is_junk, h.is_deleted) ORDER BY h.ingested_at DESC LIMIT 1)[OFFSET(0)] st
        FROM buckets b JOIN hist h ON h.d <= b.end_d WHERE b.end_d >= "2026-08-29" GROUP BY 1, 2, 3) b
  GROUP BY 1, 2),
J AS (SELECT kind, start_d, end_d FROM buckets),
a_tk AS (SELECT kind, start_d, COUNTIF(NOT junk) tickets, SAFE_DIVIDE(COUNTIF(junk), COUNT(*)) junk_pct,
    SAFE_DIVIDE(COUNTIF(NOT junk AND satisfaction_score = "good"), COUNTIF(NOT junk AND satisfaction_score IN ("good", "bad"))) csat_pct,
    SAFE_DIVIDE(COUNTIF(NOT junk AND satisfaction_score IN ("good", "bad")), COUNTIF(NOT junk AND satisfaction_score IN ("good", "bad", "offered"))) csat_response_pct,
    COUNTIF(NOT junk AND satisfaction_score IN ("good", "bad", "offered")) surveys_sent,
    COUNTIF(NOT junk AND category IN ("order___missing_item", "orders__wrong_item")) missing_wrong_tickets
  FROM J JOIN tk ON tk.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
a_fr AS (SELECT kind, start_d, APPROX_QUANTILES(frt_hrs, 100)[OFFSET(50)] frt_median_hrs,
    SAFE_DIVIDE(COUNTIF(frt_hrs <= 24), COUNT(*)) reply_24h_pct,
    SAFE_DIVIDE(COUNTIF(first_reply_at IS NOT NULL AND bd <= 2), COUNTIF(first_reply_at IS NOT NULL OR bd > 2)) reply_2bd_pct,
    COUNTIF(first_reply_at IS NULL AND bd > 2) unanswered_past_promise,
    SAFE_DIVIDE(COUNTIF(status IN ("solved", "closed") AND agent_replies = 1), COUNTIF(status IN ("solved", "closed") AND agent_replies >= 1)) one_touch_pct,
    AVG(IF(status IN ("solved", "closed") AND agent_replies >= 1, agent_replies, NULL)) replies_per_ticket
  FROM J JOIN fr_bd ON fr_bd.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
a_rep AS (SELECT kind, start_d, SUM(n) agent_replies FROM J JOIN rep ON rep.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
a_dmg AS (SELECT kind, start_d, COUNT(*) damage_tickets, SAFE_DIVIDE(COUNTIF(box = "damaged"), COUNT(*)) box_damaged_pct,
    SAFE_DIVIDE(COUNTIF(box = "fine"), COUNT(*)) box_fine_pct, SAFE_DIVIDE(COUNTIF(photo), COUNT(*)) damage_photo_pct
  FROM J JOIN dmg_c ON dmg_c.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
a_so AS (SELECT kind, start_d, COUNT(*) shopify_orders FROM J JOIN so ON so.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
a_sd AS (SELECT kind, start_d, COUNT(*) shopify_damage_tickets FROM J JOIN shop_dmg ON shop_dmg.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
a_ship AS (SELECT kind, start_d, SAFE_DIVIDE(COUNTIF(ship_bd <= 2), COUNT(*)) ship_2bd_pct, AVG(ship_bd) ship_bd_avg
  FROM J JOIN so_bd ON so_bd.d BETWEEN J.start_d AND J.end_d GROUP BY 1, 2),
agg AS (
  SELECT J.kind, J.start_d, J.end_d, DATE_DIFF(J.end_d, J.start_d, DAY) + 1 AS days, a_tk.* EXCEPT (kind, start_d), a_fr.* EXCEPT (kind, start_d),
    a_rep.agent_replies, a_dmg.* EXCEPT (kind, start_d), a_so.shopify_orders, a_sd.shopify_damage_tickets, a_ship.* EXCEPT (kind, start_d)
  FROM J LEFT JOIN a_tk USING (kind, start_d) LEFT JOIN a_fr USING (kind, start_d) LEFT JOIN a_rep USING (kind, start_d)
  LEFT JOIN a_dmg USING (kind, start_d) LEFT JOIN a_so USING (kind, start_d) LEFT JOIN a_sd USING (kind, start_d) LEFT JOIN a_ship USING (kind, start_d))
SELECT a.kind, FORMAT_DATE("%Y-%m-%d", a.start_d) AS period_start, FORMAT_DATE("%Y-%m-%d", a.end_d) AS period_end, m.metric, ROUND(m.value, 4) AS value
FROM agg a LEFT JOIN queue q USING (kind, start_d),
UNNEST([
  STRUCT("tickets_per_day" AS metric, tickets / days AS value),
  ("junk_pct", junk_pct), ("frt_median_hrs", frt_median_hrs), ("reply_24h_pct", reply_24h_pct),
  ("reply_2bd_pct", reply_2bd_pct), ("unanswered_past_promise", unanswered_past_promise),
  ("one_touch_pct", one_touch_pct), ("replies_per_ticket", replies_per_ticket),
  ("agent_replies_per_day", agent_replies / days), ("open_queue", q.open_queue),
  ("csat_pct", csat_pct), ("csat_response_pct", csat_response_pct), ("surveys_sent", surveys_sent),
  ("damage_tickets", damage_tickets), ("missing_wrong_tickets", missing_wrong_tickets),
  ("damage_per_100_orders", SAFE_DIVIDE(shopify_damage_tickets, shopify_orders) * 100),
  ("box_damaged_pct", box_damaged_pct), ("box_fine_pct", box_fine_pct), ("damage_photo_pct", damage_photo_pct),
  ("ship_2bd_pct", ship_2bd_pct), ("ship_bd_avg", ship_bd_avg)
]) m
ORDER BY kind, period_start, metric

-- Per day since 2025-12-01: public agent replies written, and the open queue at day end (history starts 2026-08-29).
WITH days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY("2025-12-01", CURRENT_DATE("America/New_York"))) d),
rep AS (
  SELECT DATE(c.created_at, "America/New_York") AS d, COUNT(*) AS n
  FROM `americanflat.zendesk.ticket_comments` c JOIN `americanflat.zendesk.users` u ON u.user_id = c.author_id
  WHERE c.public AND u.role IN ("agent", "admin") GROUP BY 1),
q AS (
  SELECT days.d, h.ticket_id, ARRAY_AGG(STRUCT(h.status, h.is_junk, h.is_deleted) ORDER BY h.ingested_at DESC LIMIT 1)[OFFSET(0)] AS st
  FROM days JOIN `americanflat.zendesk.tickets_history` h ON DATE(h.ingested_at, "America/New_York") <= days.d
  WHERE days.d >= "2026-08-29" GROUP BY 1, 2),
queue AS (
  SELECT d, COUNTIF(st.status IN ("new", "open", "pending") AND NOT COALESCE(st.is_junk, FALSE) AND NOT COALESCE(st.is_deleted, FALSE)) AS open_queue
  FROM q GROUP BY 1)
SELECT FORMAT_DATE("%Y-%m-%d", days.d) AS day, COALESCE(rep.n, 0) AS agent_replies, queue.open_queue
FROM days LEFT JOIN rep USING (d) LEFT JOIN queue USING (d)
ORDER BY day

-- Satisfaction surveys and ratings per month since Dec 2025 (junk and deleted tickets excluded).
SELECT FORMAT_DATE("%Y-%m", DATE(created_at, "America/New_York")) AS month,
  COUNTIF(satisfaction_score IN ("offered", "good", "bad")) AS surveys_sent,
  COUNTIF(satisfaction_score = "good") AS good,
  COUNTIF(satisfaction_score = "bad") AS bad
FROM `americanflat.zendesk.tickets`
WHERE NOT COALESCE(is_deleted, FALSE) AND status != "deleted" AND NOT COALESCE(is_junk, FALSE) AND created_at >= "2025-12-01"
GROUP BY 1 ORDER BY 1

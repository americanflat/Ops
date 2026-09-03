-- REPAIR: remove duplicate rows created by loading clean tracking numbers into
-- a table that still held escaped ones.
--
-- What happened (2026-09-03). The loader learned to strip Stamps.com's ="..."
-- wrapper, but the rows already in the table still carried it and the backfill
-- had not been run. The MERGE joins on tracking_number, so `94...` never
-- matched the stored `="94..."`: all 5,420 USPS labels INSERTED as new rows
-- instead of updating. The table went 20,528 -> 25,948 rows and $239,109.04 ->
-- $297,557.98, with every USPS label present twice.
--
-- Use this INSTEAD OF stamps_unescape_backfill.sql when duplicates already
-- exist. Unescaping the old rows here would leave two rows with the same
-- tracking number rather than fixing anything -- the clean copies already hold
-- the data, so the escaped copies are what has to go.
--
-- Run steps 1 -> 2 -> 3 in order. Step 1 must confirm every escaped row has a
-- clean counterpart before you delete anything.

-- ---------------------------------------------------------------------------
-- 1. Safety check. escaped_without_clean_twin MUST be 0.
--    A non-zero value means some escaped row is the ONLY copy of its label, so
--    deleting it would lose data. Stop and investigate instead.
-- ---------------------------------------------------------------------------
WITH clean_labels AS (
  SELECT DISTINCT tracking_number
  FROM `americanflat.finance.stamps_shipping_costs`
  WHERE NOT STARTS_WITH(tracking_number, '=')
)
SELECT
  COUNTIF(STARTS_WITH(t.tracking_number, '='))                AS escaped_rows,
  COUNTIF(STARTS_WITH(t.tracking_number, '=') AND c.tracking_number IS NULL)
                                                              AS escaped_without_clean_twin
FROM `americanflat.finance.stamps_shipping_costs` t
LEFT JOIN clean_labels c
  ON REGEXP_REPLACE(t.tracking_number, r'^="(.*)"$', r'\1') = c.tracking_number;

-- ---------------------------------------------------------------------------
-- 2. Drop the escaped copies. Their clean twins carry the same labels.
-- ---------------------------------------------------------------------------
DELETE FROM `americanflat.finance.stamps_shipping_costs`
WHERE STARTS_WITH(tracking_number, '=');

-- ---------------------------------------------------------------------------
-- 3. Verify. Expect 20528 / 20528 / 0 / 0.
--    total_cost will read 235644.87 at this point, NOT 239109.04: the surviving
--    rows came from a glob load whose file ordering kept stale weekly amounts.
--    Fix that by re-running the loader with ONLY the comprehensive export:
--
--      python3 tools/stamps_shipping_costs_load.py load --no-impersonate \
--        ~/Downloads/PrintHistory_1003346887_4.30.2026_to_8.31.2026.csv
--
--    The MERGE then restores the post-audit amounts and total_cost returns to
--    239109.04.
-- ---------------------------------------------------------------------------
SELECT
  COUNT(*)                                    AS row_count,
  COUNT(DISTINCT tracking_number)             AS label_count,
  COUNTIF(STARTS_WITH(tracking_number, '='))  AS escaped_rows,
  COUNT(*) - COUNT(DISTINCT REGEXP_REPLACE(tracking_number, r'^="(.*)"$', r'\1'))
                                              AS duplicate_rows,
  FORMAT('%.2f', SUM(amount_paid))            AS total_cost
FROM `americanflat.finance.stamps_shipping_costs`;

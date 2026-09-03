-- One-time backfill: strip Stamps.com's spreadsheet escaping from rows already
-- loaded before tools/stamps_shipping_costs_load.py learned to do it.
--
-- Stamps.com wraps long numerics as ="9400150105800099724475" so Excel will not
-- render them in scientific notation and truncate them. Stored verbatim they
-- are not usable values: an escaped tracking number joins to nothing in the
-- 3PL, FedEx or EDI data, and the escaped and bare forms of one label do not
-- dedupe or MERGE against each other.
--
-- Measured on 2026-09-03 against 20,528 rows:
--   tracking_number   5,420 escaped  (every USPS row)
--   to_zip           20,528 escaped  (every row)
--   order_id, reference_1, cost_code   0 escaped
--
-- Run steps 1 and 2 in order. Step 1 must return zero rows before you run 2.

-- ---------------------------------------------------------------------------
-- 1. Safety check: would unescaping collide two rows onto one tracking number?
--    Expect ZERO rows. Any row returned means unescaping would merge two
--    distinct labels, and the UPDATE below would corrupt them - stop and
--    investigate instead.
-- ---------------------------------------------------------------------------
SELECT
  REGEXP_REPLACE(tracking_number, r'^="(.*)"$', r'\1') AS unescaped,
  COUNT(*)                                             AS collisions
FROM `americanflat.finance.stamps_shipping_costs`
GROUP BY unescaped
HAVING COUNT(*) > 1;

-- ---------------------------------------------------------------------------
-- 2. The backfill. Idempotent: the regex only matches a wrapper, so a second
--    run changes nothing.
-- ---------------------------------------------------------------------------
UPDATE `americanflat.finance.stamps_shipping_costs`
SET
  tracking_number = REGEXP_REPLACE(tracking_number, r'^="(.*)"$', r'\1'),
  to_zip          = REGEXP_REPLACE(to_zip,          r'^="(.*)"$', r'\1'),
  to_name         = REGEXP_REPLACE(to_name,         r'^="(.*)"$', r'\1'),
  order_id        = REGEXP_REPLACE(order_id,        r'^="(.*)"$', r'\1'),
  reference_1     = REGEXP_REPLACE(reference_1,     r'^="(.*)"$', r'\1'),
  cost_code       = REGEXP_REPLACE(cost_code,       r'^="(.*)"$', r'\1')
WHERE STARTS_WITH(tracking_number, '=')
   OR STARTS_WITH(IFNULL(to_zip, ''), '=')
   OR STARTS_WITH(IFNULL(to_name, ''), '=')
   OR STARTS_WITH(IFNULL(order_id, ''), '=')
   OR STARTS_WITH(IFNULL(reference_1, ''), '=')
   OR STARTS_WITH(IFNULL(cost_code, ''), '=');

-- ---------------------------------------------------------------------------
-- 3. Verify. All three escaped_* columns must be 0, rows must equal labels,
--    and total_cost must be unchanged at 239109.04. row_count/label_count
--    rather than rows/labels because `rows` is reserved. This touches identifiers
--    only and must never move the money.
-- ---------------------------------------------------------------------------
SELECT
  COUNT(*)                                        AS row_count,
  COUNT(DISTINCT tracking_number)                 AS label_count,
  COUNTIF(STARTS_WITH(tracking_number, '='))      AS escaped_tracking,
  COUNTIF(STARTS_WITH(IFNULL(to_zip, ''), '='))   AS escaped_zip,
  FORMAT('%.2f', SUM(amount_paid))                AS total_cost,
  -- Sanity on shape: USPS is 22 digits for Ground Advantage, 25/29 for some
  -- First-Class IMpb; UPS is 1Z + 16. Nothing should be non-alphanumeric now.
  COUNTIF(NOT REGEXP_CONTAINS(tracking_number, r'^[A-Za-z0-9]+$')) AS still_dirty
FROM `americanflat.finance.stamps_shipping_costs`;

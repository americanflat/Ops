-- Carrier shipping invoices — americanflat.finance.shipping_invoices
--
-- One row per charge line: a tracking number can carry several charges
-- (base transportation, fuel, residential, address correction ...), and each
-- of those is its own row. A carrier that bills one flat amount per package
-- (Stamps.com) therefore produces exactly one row per label.
--
-- Loaded by tools/shipping_invoice_load.py from the weekly Stamps.com, FedEx
-- and UPS invoice CSVs. See docs/shipping-invoices.md.
--
-- Run this once, as someone holding bigquery.tables.create on `finance`.
-- The warehouse credential the loader uses can INSERT/DELETE here but cannot
-- create the table itself.

CREATE TABLE IF NOT EXISTS `americanflat.finance.shipping_invoices`
(
  row_id             STRING  NOT NULL OPTIONS(description="SHA-256 of the identifying fields plus the line's sequence within its file. Stable across re-parses of the same file, so a reload is detectable"),
  carrier            STRING  NOT NULL OPTIONS(description="Billing source: 'FedEx', 'UPS' or 'Stamps.com'"),
  invoice_number     STRING  OPTIONS(description="Carrier invoice number. NULL for Stamps.com print-history exports, which are not invoices"),
  invoice_date       DATE    OPTIONS(description="Date the carrier issued the invoice"),
  account_number     STRING  OPTIONS(description="Billed account / payor account at the carrier"),
  ship_date          DATE    OPTIONS(description="Date the package shipped. Partition column — the field almost every question filters on"),
  tracking_number    STRING  OPTIONS(description="Carrier tracking number, digits only where the carrier prints separators"),
  service            STRING  OPTIONS(description="Service level as printed (FedEx Home Delivery, Priority Mail, UPS Ground ...)"),
  charge_description STRING  OPTIONS(description="Charge as the carrier labels it: 'Fuel Surcharge', 'Residential', 'Postage' ..."),
  charge_category    STRING  OPTIONS(description="charge_description bucketed to base / fuel / surcharge / adjustment / other, so charges roll up across carriers that name the same fee differently"),
  amount             NUMERIC OPTIONS(description="Charge amount in currency. Negative for credits and refunds"),
  adjusted_amount    NUMERIC OPTIONS(description="Post-audit corrected amount where the carrier prints one (Stamps.com 'Adjusted Amount'). NULL means no adjustment was reported, not zero"),
  currency           STRING  OPTIONS(description="ISO currency code, 'USD' unless the file says otherwise"),
  weight_lbs         NUMERIC OPTIONS(description="Actual / entered weight in pounds"),
  billed_weight_lbs  NUMERIC OPTIONS(description="Weight the carrier actually billed (dimensional or rated). Differs from weight_lbs on DIM-rated packages"),
  zone               STRING  OPTIONS(description="Rate zone as printed"),
  reference_1        STRING  OPTIONS(description="First shipper reference. FedEx 'Original Customer Reference', UPS 'Shipment Reference Number 1', Stamps.com 'Reference 1'"),
  reference_2        STRING  OPTIONS(description="Second shipper reference. FedEx 'Original Ref#2', UPS 'Shipment Reference Number 2', Stamps.com 'Printed Message' — which is where the marketplace usually shows up"),
  reference_3        STRING  OPTIONS(description="Third shipper reference. FedEx 'Original Ref#3/PO Number' — the PO the 3PL match runs on"),
  order_id           STRING  OPTIONS(description="Order identifier where the carrier carries one (Stamps.com 'Order ID')"),
  cost_code          STRING  OPTIONS(description="Stamps.com cost code"),
  recipient_name     STRING,
  recipient_city     STRING,
  recipient_state    STRING,
  recipient_zip      STRING,
  recipient_country  STRING,
  shipper_city       STRING,
  raw                STRING  OPTIONS(description="Every column of the source row that has no home above, as a JSON object (parse with PARSE_JSON). The safety net: a carrier adding or renaming a column still lands losslessly and stays queryable"),
  source_file        STRING  NOT NULL OPTIONS(description="Basename of the uploaded CSV. The loader's duplicate guard keys on this"),
  ingested_at        TIMESTAMP NOT NULL OPTIONS(description="When the row was loaded (UTC)"),
  ingested_by        STRING  OPTIONS(description="Who ran the load")
)
PARTITION BY ship_date
CLUSTER BY carrier, tracking_number
OPTIONS(
  description="Weekly carrier shipping invoices from Stamps.com, FedEx and UPS, one row per charge line. Loaded by tools/shipping_invoice_load.py in americanflat/Ops; see docs/shipping-invoices.md. Partitioned by ship_date, clustered by carrier and tracking_number."
);

-- One row per package, for joining against 3PL shipped-order reports —
-- which are keyed on tracking, not on charge line.
CREATE OR REPLACE VIEW `americanflat.finance.shipping_invoice_packages` AS
SELECT
  carrier,
  tracking_number,
  ANY_VALUE(invoice_number)                                   AS invoice_number,
  MAX(invoice_date)                                           AS invoice_date,
  MAX(ship_date)                                              AS ship_date,
  ANY_VALUE(service)                                          AS service,
  SUM(amount)                                                 AS total_amount,
  SUM(IF(charge_category = 'base',      amount, 0))           AS base_amount,
  SUM(IF(charge_category = 'fuel',      amount, 0))           AS fuel_amount,
  SUM(IF(charge_category = 'surcharge', amount, 0))           AS surcharge_amount,
  SUM(IF(charge_category = 'adjustment', amount, 0))          AS adjustment_amount,
  COUNT(*)                                                    AS charge_lines,
  MAX(weight_lbs)                                             AS weight_lbs,
  MAX(billed_weight_lbs)                                      AS billed_weight_lbs,
  ANY_VALUE(zone)                                             AS zone,
  ANY_VALUE(reference_1)                                      AS reference_1,
  ANY_VALUE(reference_2)                                      AS reference_2,
  ANY_VALUE(reference_3)                                      AS reference_3,
  ANY_VALUE(order_id)                                         AS order_id,
  ANY_VALUE(recipient_state)                                  AS recipient_state,
  ANY_VALUE(recipient_zip)                                    AS recipient_zip,
  STRING_AGG(DISTINCT source_file, ', ')                      AS source_files
FROM `americanflat.finance.shipping_invoices`
WHERE tracking_number IS NOT NULL
GROUP BY carrier, tracking_number;

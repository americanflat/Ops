-- Stedi 940 warehouse shipping orders, one row per 940 transaction.
-- Loaded by tools/stedi_940_load.py. Run once in the BigQuery console to create.
CREATE TABLE IF NOT EXISTS `americanflat.warehouse.shipments_940_raw`
(
  transactionId STRING NOT NULL OPTIONS(description='Stedi transaction id of the 940 - natural key'),
  fileExecutionId STRING OPTIONS(description='Stedi file execution that produced the 940'),
  processedAt TIMESTAMP OPTIONS(description='When Stedi processed (sent) the 940'),
  partnershipId STRING OPTIONS(description='Stedi partnership, e.g. americanflat_Taylored-SC'),
  warehouse STRING OPTIONS(description='FON / NJ / SC / NL, from the partnership - same codes as warehouse.shipments_945'),
  orderStatusCode STRING OPTIONS(description='W05-01: N = new order'),
  depositorOrderNumber STRING OPTIONS(description='W05-02 - the order number the warehouse echoes back as the 945 orderNumber'),
  purchaseOrderNumber STRING OPTIONS(description='W05-03 - marketplace PO / Shopify order id; often FedEx Ref#3'),
  marketplaceRaw STRING OPTIONS(description='N9*IT - the marketplace as sent: ACNDA_TGTPL, AMAZON.COM, WAYFAIR LLC, SHOPIFY, WALMARTCOM, MACYS, AMAZON.FBA, AMAZON.VC. warehouse.shipments_940 maps it to a channel code'),
  department STRING OPTIONS(description="N9*DP - department; only Target fills it ('Target')"),
  internalVendorNumber STRING OPTIONS(description='N9*IA'),
  customerOrderNumber STRING OPTIONS(description='N9*CO'),
  dataSource STRING OPTIONS(description='N9*S2'),
  n9 JSON OPTIONS(description='Every header N9 as {qualifier: value}'),
  shipToName STRING OPTIONS(description='N1*ST name'),
  shipToAddress1 STRING,
  shipToCity STRING,
  shipToState STRING,
  shipToPostalCode STRING,
  shipToCountry STRING,
  orderedDate DATE OPTIONS(description='G62*52'),
  requestedShipDate DATE OPTIONS(description='G62*10'),
  cancelAfterDate DATE OPTIONS(description='G62*01'),
  carrierScac STRING OPTIONS(description='W66-10'),
  routing STRING OPTIONS(description='W66-05, e.g. FDX90'),
  transportMethod STRING OPTIONS(description='W66-02'),
  paymentMethod STRING OPTIONS(description='W66-01, e.g. PP'),
  lines ARRAY<STRUCT<
    lineSeq INT64 OPTIONS(description='LX number'),
    sku STRING OPTIONS(description='W01 VN'),
    customerItemNumber STRING OPTIONS(description='W01 CN'),
    upc STRING OPTIONS(description='W01-03, else W01 UP'),
    quantityOrdered FLOAT64,
    uom STRING,
    description STRING OPTIONS(description='G69'),
    lineItemRef STRING OPTIONS(description='line N9*LI'),
    amount NUMERIC OPTIONS(description='AMT*LI')
  >> OPTIONS(description='W01 line items'),
  _ingested_at TIMESTAMP OPTIONS(description='When the row was loaded (UTC)'),
  _ingested_by STRING OPTIONS(description='gcloud account that ran the load')
)
PARTITION BY DATE(processedAt)
CLUSTER BY depositorOrderNumber, purchaseOrderNumber
OPTIONS(
  description='One row per Stedi 940 warehouse shipping order sent to Taylored (FON/NJ/SC) and Yusen (NL). marketplaceRaw (N9*IT) names the marketplace even when no 945 ever came back. Loaded by tools/stedi_940_load.py in americanflat/Ops.'
);

-- Marketplace-coded view. The N9*IT -> channel mapping lives here, not in the
-- table, so it can change without a reload. Codes match warehouse.channel_keys.
CREATE OR REPLACE VIEW `americanflat.warehouse.shipments_940` AS
SELECT
  *,
  CASE
    WHEN marketplaceRaw = 'ACNDA_TGTPL' OR department = 'Target' THEN 'TARG'
    WHEN marketplaceRaw = 'AMAZON.COM'  THEN 'AMZC'
    WHEN marketplaceRaw = 'AMAZON.VC'   THEN 'AMZVC'
    WHEN marketplaceRaw = 'AMAZON.FBA'  THEN 'AMZFBA'
    WHEN marketplaceRaw = 'WAYFAIR LLC' THEN 'WAYF'
    WHEN marketplaceRaw = 'SHOPIFY'     THEN 'SHIPSTATION'
    WHEN marketplaceRaw = 'WALMARTCOM'  THEN 'WALC'
    WHEN marketplaceRaw = 'MACYS'       THEN 'MACY'
    WHEN marketplaceRaw = 'KOHLS'       THEN 'KOHLS'
    ELSE marketplaceRaw
  END AS channel
-- A 940 can be resent or redirected to another warehouse, so one order can
-- have several rows; pick the latest per order with processedAt.
FROM `americanflat.warehouse.shipments_940_raw`;

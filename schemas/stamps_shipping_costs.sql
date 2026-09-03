CREATE TABLE IF NOT EXISTS `americanflat.finance.stamps_shipping_costs`
(
  ship_date DATE OPTIONS(description='Label ship/print date, normalized to YYYY-MM-DD from the source MM/DD/YYYY'),
  tracking_number STRING NOT NULL OPTIONS(description='Carrier tracking number - natural key. USPS 94.., UPS 1Z..'),
  carrier STRING OPTIONS(description='Carrier that moved the parcel: USPS or UPS. Stamps.com is the label vendor, not the carrier'),
  service STRING OPTIONS(description='Service level as printed, e.g. \'USPS Ground Advantage\' or \'UPS Ground Saver\''),
  weight_lb NUMERIC OPTIONS(description='Billed weight in pounds. Parsed from a decimal or an \'Xlb Yoz\' string. Often blank in the consolidated export'),
  amount_paid NUMERIC OPTIONS(description='Shipping cost paid for this label. CAN BE NEGATIVE - refunds and postage corrections post as negative rows'),
  adjusted_amount NUMERIC OPTIONS(description='Post-audit adjusted amount when the carrier reweighed or rerated the parcel; null when never adjusted'),
  order_id STRING OPTIONS(description='Stamps.com Order ID. Join key to 3PL shipped-order reports; absent from the consolidated export'),
  reference_1 STRING OPTIONS(description='Reference 1 field - usually the marketplace order or PO number. Join key for marketplace cost attribution'),
  cost_code STRING OPTIONS(description='Stamps.com cost code, used to tag the paying business unit or marketplace'),
  to_name STRING OPTIONS(description='Recipient name as printed on the label'),
  to_zip STRING OPTIONS(description='Recipient ZIP. STRING not INTEGER - leading zeros are significant'),
  source_file STRING OPTIONS(description='Originating Stamps.com PrintHistory CSV filename. The idempotency key: a reload of one file replaces exactly its own rows'),
  ingested_at TIMESTAMP OPTIONS(description='When the row was loaded (UTC)'),
  ingested_by STRING OPTIONS(description='gcloud account that ran the load')
)
PARTITION BY ship_date
CLUSTER BY carrier, tracking_number
OPTIONS(
  description='One row per Stamps.com shipping label. amount_paid is the cost of that label; it can be negative (refunds and postage corrections). Loaded by tools/stamps_shipping_costs_load.py in americanflat/Ops.'
);

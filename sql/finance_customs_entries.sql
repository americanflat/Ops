-- finance.customs_entries: CBP Form 7501 entry summaries, one row per entry.
-- Console-ready equivalent of `python3 tools/customs_entries_load.py --init`
-- (generated from schemas/customs_entries.json - keep the two in step).
-- After running it, also run sql/finance_customs_entry_tariffs.sql for the view.
CREATE TABLE IF NOT EXISTS `americanflat.finance.customs_entries` (
  entry_number STRING NOT NULL OPTIONS(description="CBP entry number as printed in box 1, e.g. 8NW-1591298-8 - natural key, one row per entry"),
  filer_code STRING OPTIONS(description="First three characters of the entry number: the customs broker's CBP filer code"),
  entry_type STRING OPTIONS(description="Box 2 entry type code, e.g. 01 (consumption)"),
  entry_type_detail STRING OPTIONS(description="Box 2 qualifier printed after the code, e.g. ABI/P"),
  summary_date DATE OPTIONS(description="Box 3 - date the entry summary was filed"),
  entry_date DATE OPTIONS(description="Box 7 - date of entry. Decides which fiscal year's MPF minimum/maximum applies"),
  import_date DATE OPTIONS(description="Box 11"),
  export_date DATE OPTIONS(description="Box 15"),
  surety_number STRING OPTIONS(description="Box 4"),
  bond_type STRING OPTIONS(description="Box 5 - 8 is continuous bond"),
  port_code STRING OPTIONS(description="Box 6 - port of entry (2704 = Los Angeles)"),
  importing_carrier STRING OPTIONS(description="Box 8 - vessel / carrier as printed"),
  mode_of_transport STRING OPTIONS(description="Box 9 - 11 is vessel, containerized"),
  country_of_origin STRING OPTIONS(description="Box 10 (ISO-2)"),
  bill_of_lading STRING OPTIONS(description="Box 12 - master B/L. Join key to drayage, demurrage and Yusen invoices"),
  manufacturer_id STRING OPTIONS(description="Box 13 MID"),
  exporting_country STRING OPTIONS(description="Box 14 (ISO-2)"),
  foreign_port_of_lading STRING OPTIONS(description="Box 19 (Schedule K code)"),
  us_port_of_unlading STRING OPTIONS(description="Box 20"),
  location_of_goods STRING OPTIONS(description="Box 25 - FIRMS code / terminal"),
  importer_number STRING OPTIONS(description="Box 27 - importer of record number"),
  importer_name STRING OPTIONS(description="Box 30 - importer of record"),
  broker_name STRING OPTIONS(description="Box 46 - customs broker that filed the entry"),
  broker_file_number STRING OPTIONS(description="Box 47 - broker's own file number"),
  po_numbers ARRAY<STRING> OPTIONS(description="PO numbers from the box 47 reference (Ref: PO#...)"),
  shipment_type STRING OPTIONS(description="stock (own inventory) or amazon_di (Amazon Direct Import) - valued differently, so value checks branch on it. Null when not yet known"),
  total_entered_value NUMERIC OPTIONS(description="Box 39 - total entered value as declared on the form"),
  duty NUMERIC OPTIONS(description="Box 41"),
  tax NUMERIC OPTIONS(description="Box 42"),
  other_fees NUMERIC OPTIONS(description="Box 43 - MPF + HMF + any other fees"),
  total NUMERIC OPTIONS(description="Box 44 - duty + tax + other. What the importer owes CBP for this entry"),
  fees ARRAY<STRUCT<
    code STRING OPTIONS(description="CBP class code, e.g. 499 MPF, 501 HMF"),
    name STRING,
    amount NUMERIC
  >> OPTIONS(description="Other Fee Summary for block 43, as charged (MPF here is after the min/max is applied)"),
  lines ARRAY<STRUCT<
    line_number INTEGER,
    description STRING OPTIONS(description="Merchandise description: that of the line's commodity (non chapter 99) tariff, else the line heading"),
    invoice_number STRING OPTIONS(description="Commercial invoice the line belongs to"),
    entered_value NUMERIC OPTIONS(description="Sum of the line's tariff entered values (chapter 99 rows carry 0)"),
    charges NUMERIC OPTIONS(description="Box 36B - freight and other charges excluded from entered value"),
    tariffs ARRAY<STRUCT<
      hts_number STRING,
      description STRING,
      gross_weight_kg NUMERIC,
      quantity NUMERIC,
      quantity_uom STRING,
      entered_value NUMERIC,
      rate STRING OPTIONS(description="Rate as printed: 12.5%, Free, or a specific rate"),
      duty NUMERIC
    >>,
    line_fees ARRAY<STRUCT<
      code STRING,
      name STRING,
      rate STRING,
      amount NUMERIC
    >> OPTIONS(description="Per-line MPF / HMF before the entry-level MPF min/max")
  >> OPTIONS(description="Box 31 entry lines"),
  commercial_invoices ARRAY<STRUCT<
    invoice_number STRING,
    invoice_value NUMERIC OPTIONS(description="Invoice Value as printed"),
    freight_deduction NUMERIC OPTIONS(description="(-) International Freight"),
    duty_deduction NUMERIC OPTIONS(description="(-) Duty, Fees and Taxes - the duty the invoice price assumed, e.g. on DDP terms"),
    other_deductions NUMERIC OPTIONS(description="Any other (-) adjustment, summed"),
    entered_value NUMERIC OPTIONS(description="Total Entered Value (Invoice)")
  >> OPTIONS(description="Commercial invoice reconciliation printed on the continuation sheet"),
  validation_status STRING OPTIONS(description="ok, or flagged when any check below raised a flag. Errors never load without --allow-mismatch"),
  validation_findings ARRAY<STRUCT<
    check_name STRING,
    severity STRING OPTIONS(description="error (the form does not add up - usually a parse problem), flag (the entry looks wrong), info"),
    message STRING,
    expected NUMERIC,
    actual NUMERIC
  >> OPTIONS(description="Every check that did not pass, as of load time"),
  raw_text STRING OPTIONS(description="Full layout text extracted from the PDF - the safety net for anything not mapped above"),
  source_file STRING OPTIONS(description="Original PDF filename"),
  source_sha256 STRING OPTIONS(description="SHA-256 of the PDF. Same entry + same hash = already loaded"),
  ingested_at TIMESTAMP OPTIONS(description="When the row was loaded (UTC)"),
  ingested_by STRING OPTIONS(description="gcloud account that ran the load")
)
PARTITION BY DATE_TRUNC(entry_date, MONTH)
CLUSTER BY entry_number, bill_of_lading
OPTIONS(description="CBP Form 7501 customs entries - one row per entry. Loader: tools/customs_entries_load.py");

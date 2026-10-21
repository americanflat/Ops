-- finance.customs_entry_tariffs: one row per HTS row of every customs entry,
-- flattened from finance.customs_entries so duty can be queried by HTS number,
-- container B/L or PO without UNNESTing by hand.
--
-- Created by `python3 tools/customs_entries_load.py --init`. Re-running this
-- file in the console is also safe.
CREATE OR REPLACE VIEW `americanflat.finance.customs_entry_tariffs` AS
SELECT
  e.entry_number,
  e.entry_date,
  e.bill_of_lading,
  e.broker_name,
  e.po_numbers,
  l.line_number,
  l.description AS line_description,
  l.invoice_number,
  t.hts_number,
  STARTS_WITH(t.hts_number, '99') AS is_chapter_99,
  t.description AS tariff_description,
  t.quantity,
  t.quantity_uom,
  t.entered_value,
  t.rate,
  t.duty,
  e.validation_status
FROM `americanflat.finance.customs_entries` e,
  UNNEST(e.lines) l,
  UNNEST(l.tariffs) t

CREATE TABLE IF NOT EXISTS `americanflat.finance.freight_documents_raw`
(
  sha256 STRING NOT NULL OPTIONS(description='SHA-256 (hex) of the raw GCS object: the PDF bytes, or the exported email JSON. Natural key - one row per distinct document'),
  doc_type STRING NOT NULL OPTIONS(description='Document type the extractor settled on. Same as routed_doc_type, except a PDF whose text says CBP Form 7501 is customs_7501 wherever it was filed'),
  routed_doc_type STRING NOT NULL OPTIONS(description='Document type from the GCS path, i.e. what the Gmail export guessed from the search, filename and subject'),
  source_kind STRING NOT NULL OPTIONS(description='pdf_attachment or email_body'),
  parser STRING NOT NULL OPTIONS(description='What produced the record, e.g. pdftotext-layout, pdftotext-layout+documentai-form-parser, pdftotext-layout+customs_7501, email-body'),
  parser_version STRING OPTIONS(description='Image version (git short sha) of the extract service that wrote the record'),
  parse_status STRING NOT NULL OPTIONS(description='parsed (a typed parser read it - only customs_7501 today), unparsed (text extracted, no typed parser for this layout yet) or failed (no text, or the typed parser rejected it; see parse_error and quarantine/)'),
  parse_error STRING OPTIONS(description='Why parse_status is failed, or a non-fatal extraction note (e.g. Document AI refused the file)'),
  page_count INT64 OPTIONS(description='Pages in the PDF (pdfinfo). Null for email bodies'),
  full_text STRING OPTIONS(description='pdftotext -layout output for PDFs (the text the 7501 parser reads); the plain-text body for emails'),
  docai_text STRING OPTIONS(description='Document AI Form Parser OCR text. Null when Document AI was not used (7501s, emails, processor not configured)'),
  tables JSON OPTIONS(description='Document AI tables: [{page, header_rows: [[cell...]], body_rows: [[cell...]]}]. The same tables are written as CSV next to the NDJSON'),
  entities JSON OPTIONS(description='Document AI form fields and entities: {form_fields: [{page, name, value, confidence}], entities: [{type, text, confidence}]}'),
  typed JSON OPTIONS(description='Typed parse. customs_7501 only: the parse_7501() result plus validate() findings from tools/customs_entries_load.py. Null otherwise'),
  gcs_uri STRING NOT NULL OPTIONS(description='gs:// URI of the raw object (provenance)'),
  processed_uri STRING NOT NULL OPTIONS(description='gs:// URI of the NDJSON this row was loaded from'),
  source_file STRING OPTIONS(description='Original attachment filename, or the Gmail message id for an email body'),
  gmail_message_id STRING OPTIONS(description='Gmail message id the document was exported from (first one seen, for a PDF sent more than once)'),
  gmail_thread_id STRING OPTIONS(description='Gmail thread id'),
  email_date TIMESTAMP OPTIONS(description='Gmail internal date of the message'),
  email_from STRING OPTIONS(description='From header as received'),
  email_subject STRING OPTIONS(description='Subject header as received'),
  search_query STRING OPTIONS(description='The Gmail search that matched the thread'),
  meta JSON OPTIONS(description='The full .meta.json (PDFs) or export envelope (emails) written by the Apps Script'),
  extracted_at TIMESTAMP NOT NULL OPTIONS(description='When the extract service wrote the NDJSON (UTC). A --full reload only overwrites a row with a newer extraction'),
  ingested_at TIMESTAMP NOT NULL OPTIONS(description='When the row was loaded (UTC)'),
  ingested_by STRING NOT NULL OPTIONS(description='Account that ran the load (the freight-docs-load service account)')
)
CLUSTER BY doc_type, parse_status, sha256
OPTIONS(
  description='Landing table for freight documents exported from the Administrator@ Gmail: one row per distinct PDF or email body, keyed on sha256, with the raw extraction (schema on read). Typed 7501 rows go on to finance.customs_entries. Loaded by services/freight_docs_load in americanflat/Ops.'
);

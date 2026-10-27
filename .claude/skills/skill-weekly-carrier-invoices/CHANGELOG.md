# Changelog

All notable changes to this skill will be documented in this file.

## [0.1.0] - 2026-10-27

### Added
- Initial release.
- `process_invoices.py`: detects FedEx, UPS billing data, and Stamps.com print history files
  by content; writes Google Sheets append files plus `summary.json` with per-invoice totals,
  ship-date ranges, and flags (duplicate downloads, re-billed tracking numbers, unscanned and
  refunded Stamps labels, Stamps account changes, UPS line-sum vs. invoice-total mismatches).
- UPS line-level charges rolled up to one row per package, separating third-party-billed $0
  packages from packages Americanflat pays for.
- `load_fedex_to_bq.py`: appends FedEx exports to `americanflat.finance.fedex_shipping_costs`
  using the live table schema, de-duplicating on invoice number + tracking ID, dry run by
  default, service-account impersonation, no IAM operations.
- `build_message.py`: builds the weekly AP invoice message, staged as a Slack draft to
  Georgina Gallo (replaces the previous recipient, Noelia) for review before sending.

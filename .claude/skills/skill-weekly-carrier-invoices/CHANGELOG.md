# Changelog

All notable changes to this skill will be documented in this file.

## [0.2.0] - 2026-10-27

### Changed
- FedEx load now uses `scripts/carrier_invoices_load.py` (copied from `tools/carrier_invoices_load.py`
  on the `claude/fedex-ups-shipping-costs-loaders` branch) with its schemas. It fills all 26
  columns, keys rows on `row_key`, merges only new rows, verifies, and writes the load log.
  Replaces `load_fedex_to_bq.py`, which matched only 7 of 26 columns and impersonated a
  service account the cloud session can't use.
- Loader fixes: reads query JSON even when `bq` prints warnings first (they had silently
  disabled the pre-merge overlap guard and post-load verify); refuses to merge if the
  pre-merge check returns nothing; finds the signed-in identity from BigQuery when gcloud
  has no account (cloud sessions).
- `process_invoices.py`: `--skip-fedex-invoices` and `--stamps-after` limit output to this
  week's rows; strips the upload prefix from `Source File`.
- `build_message.py`: Anthony's approved layout (markdown bold, M/D dates, Stamps.com "no action
  needed" notice for tying the Auctane invoices); opens with an @-mention of the AP recipient's
  Slack member ID;
  `--ups-credit-note` adds the Yusen credit line; flags are internal and only included with
  `--with-notes`.
- SKILL.md: new steps for working out what's new (BigQuery), the UPS → Yusen (South Carolina)
  check against the Gmail thread, and addressing people by Slack member ID. Sheets append is
  now optional (the sheets stopped being updated on 2026-04-30).

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

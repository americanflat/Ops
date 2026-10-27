---
name: skill-weekly-carrier-invoices
description: >
  Runs Americanflat's weekly carrier invoice routine end to end: takes the week's FedEx invoice
  CSVs, UPS billing data CSVs, and Stamps.com print history, turns them into append files for
  the FedEx and Stamps Google Sheets, loads FedEx into BigQuery
  (americanflat.finance.fedex_shipping_costs), and drafts the weekly invoice message to AP
  (Georgina Gallo) in Slack for Anthony to review. Use this skill whenever the user drops or
  points at carrier invoice files and says anything like "here are the files from this week",
  "process this week's invoices", "run the weekly invoices", "FedEx, UPS and Stamps for this
  week", "send Georgina the invoices", or "update the shipping sheets". Trigger even if only
  one or two of the three carriers are present. Does NOT do the 3PL matching / cost-per-unit
  report (that is shipping-cost-report) or the dashboard refresh (cost-per-sku-dashboard).
---

# Weekly Carrier Invoices

Every week Anthony collects carrier invoices from FedEx, UPS, and Stamps.com. This skill turns
them into: append files for the Google Sheets, a FedEx load into BigQuery, and a drafted
Slack message to AP. Three human checkpoints: the totals, the BigQuery load, the message.

Settings (AP recipient, sheet links, BigQuery target, last-seen Stamps account) live in
`config.json`. Read it first.

## Step 1 — Gather files

Use the files the user named, attached, or pointed to (a folder is fine). Only pass **this
week's** files; folders like Downloads often hold old exports. Ask if unsure which are new.

The script identifies each file by its contents, not its name:
- **FedEx**: CSV with `Express or Ground Tracking ID` and `Net Charge Amount` headers
- **UPS**: headerless billing-data CSV starting with `2.1` (e.g. `Invoice_000000H9229J416_101026.csv`)
- **Stamps.com**: `PrintHistory_<account>_<dates>.csv` starting with `Date Printed`

## Step 2 — Process

```bash
python3 <skill-dir>/scripts/process_invoices.py <files...> --out <work-dir> \
  --last-stamps-account <config.last_stamps_account>
```

Writes `fedex_append_<date>.csv`, `stamps_append_<date>.csv`, `ups_append_<date>.csv`, and
`summary.json`. Exact duplicate FedEx downloads (same invoice + tracking twice) are skipped.

Report to the user, briefly: per carrier the package/label count, total, ship-date range, and
every entry in each carrier's `flags`. Also call out:
- **Coverage gaps:** compare ship-date ranges to last week. FedEx invoices lag about 1–2 weeks;
  that's normal. A Stamps range that doesn't start the day after last week's ended is a gap.
- **UPS:** most packages are third-party billed to Amazon at $0. Say how many actually cost
  money (`paid_packages`) and the account-level fees.
- **Stamps account change:** if flagged, ask whether to update `last_stamps_account` in config.
- **Missing carriers** or **unrecognized files**.

## Step 3 — Google Sheets

The Sheets connector can't append rows, so hand the files to Anthony: open each sheet in
`config.sheets` → **File → Import → Upload → Append to current sheet**. FedEx and Stamps have
sheets; there is no UPS sheet, so the UPS file is for reference unless he asks for one.

## Step 4 — FedEx → BigQuery (sign-off required)

Dry run first:
```bash
python3 <skill-dir>/scripts/load_fedex_to_bq.py <FedEx CSV(s) from Step 1>
```
Pass the **original FedEx export(s)**, not the append file. Show the per-invoice totals and the
"already in table / new rows" counts, then ask for a yes. On yes, re-run with `--apply`.
If a column shows up under "Left NULL" or no dedupe key is found, stop and tell Anthony.
On PERMISSION DENIED, tell him which grant to request; never touch IAM.

## Step 5 — Draft the AP message (never auto-send)

```bash
python3 <skill-dir>/scripts/build_message.py <work-dir>/summary.json
```

Create it as a **Slack draft** (`slack_send_message_draft`) in a DM to
`config.ap_recipient.slack_user_id`, so Anthony reviews and sends it himself. Before drafting,
show him the text and drop any Notes lines that are internal only. If Slack isn't connected
in this session, print the message for him to paste.

## Step 6 — Wrap up

Summarize what was produced and what's still on Anthony: importing into Sheets, sending the
Slack draft. Mention that the cost-per-unit report and dashboard refresh still need this
week's 3PL shipped-order files (NJ, Fontana, SC) and are separate skills.

## Requirements
- Python 3.9+ (stdlib only)
- Google Cloud CLI (`gcloud` + `bq`) with `gcloud auth login`, for Step 4
- Slack MCP connected in Claude Code, for Step 5 (optional; falls back to printed text)

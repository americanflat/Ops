# skill-weekly-carrier-invoices

The weekly FedEx / UPS / Stamps.com invoice routine, in one skill.

## What it does
1. Detects which carrier each file is from by its contents.
2. Builds append files for the FedEx and Stamps Google Sheets (+ a UPS per-package file).
3. Flags anything odd: coverage gaps, duplicates, unscanned labels, account changes.
4. Loads FedEx into BigQuery after you approve the totals.
5. Drafts the weekly invoice message to AP (Georgina) in Slack for you to send.

## Usage (in Claude Code)
- "Here are this week's invoices" (with the files or a folder path)
- "Process the FedEx, UPS and Stamps files and draft Georgina's message"

## Setup (one-time)
- Google Cloud CLI + `gcloud auth login`, with impersonation on the writer service account.
- Slack MCP connected in Claude Code (optional).
- Edit `config.json` if the AP recipient, sheets, or BigQuery table change.

# skill-weekly-carrier-invoices

The weekly FedEx / UPS / Stamps.com invoice routine, in one skill.

## What it does
1. Detects which carrier each file is from by its contents.
2. Works out what's new this week (FedEx invoices not yet loaded, Stamps labels after the last
   loaded ship date) and builds this-week-only files plus a summary.
3. Loads FedEx into `americanflat.finance.fedex_shipping_costs` after you approve the totals,
   using the repo's carrier loader (row keys, dedupe, post-load verify, load log).
4. Checks UPS South Carolina charges against the Yusen credit thread and drafts a reply only
   if the invoice hasn't been reported yet.
5. Drafts the weekly invoice message to AP in Slack, addressed with an @-mention, for you to send.

## Usage (in Claude Code)
- "Here are this week's invoices" (with the files or a folder path)

## Setup (one-time)
- Google Cloud CLI signed in (`gcloud auth login`) with write access to `finance`, or a cloud
  session whose BigQuery identity has it.
- Slack and Gmail connected in Claude Code (optional).
- Edit `config.json` if people, sheets, or the BigQuery table change.

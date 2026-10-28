---
name: skill-weekly-carrier-invoices
description: >
  Runs Americanflat's weekly carrier invoice routine end to end: takes the week's FedEx invoice
  CSVs, UPS billing data CSVs, and Stamps.com print history, works out which invoices and labels
  are new this week, loads FedEx into BigQuery (americanflat.finance.fedex_shipping_costs),
  checks UPS charges against the open South Carolina billing thread with Yusen, and drafts the
  weekly invoice message to AP (Georgina Gallo) in Slack for Anthony to review. Use this skill
  whenever the user drops or points at carrier invoice files and says anything like "here are
  the files from this week", "process this week's invoices", "run the weekly invoices", "FedEx,
  UPS and Stamps for this week", "send Georgina the invoices", or "update the shipping sheets".
  Trigger even if only one or two of the three carriers are present. Does NOT do the 3PL
  matching / cost-per-unit report (that is shipping-cost-report) or the dashboard refresh
  (cost-per-sku-dashboard).
---

# Weekly Carrier Invoices

Every week Anthony collects carrier invoices from FedEx, UPS, and Stamps.com. This skill turns
them into: a FedEx load into BigQuery, a check of UPS charges against the Yusen credit thread,
a drafted Slack message to AP, and (optionally) append files for the Google Sheets. Human
checkpoints: the totals, the BigQuery load, every message.

Settings (people, sheet links, BigQuery target, last-seen Stamps account) live in
`config.json`. Read it first.

## Addressing people

Whenever a message is addressed to someone in Slack, address them with an @-mention of their
Slack member ID, written `<@MEMBER_ID>` (e.g. `<@U07B8VAF872>` for Georgina), never a typed
name. Member IDs live in `config.json` (`ap_recipient.slack_user_id`, and `people`). If someone
new needs addressing and their ID isn't in config, look it up (`slack_search_users`), confirm
with Anthony, and add it to `config.json`.

## Step 1 — Gather files

Use the files the user named, attached, or pointed to (a folder is fine). The script identifies
each file by its contents, not its name:
- **FedEx**: CSV with `Express or Ground Tracking ID` and `Net Charge Amount` headers
- **UPS**: headerless billing-data CSV starting with `2.1` (e.g. `Invoice_000000H9229J426_101726.csv`)
- **Stamps.com**: `PrintHistory_<account>_<dates>.csv` starting with `Date Printed`

## Step 2 — Work out what is new this week

FedEx exports often repeat last week's invoices, and Stamps print history usually spans several
weeks. "This week" means what hasn't already been loaded/sent. Check BigQuery (read-only):

```sql
-- FedEx invoices in the file that are already loaded
SELECT DISTINCT invoice_number FROM `americanflat.finance.fedex_shipping_costs`
WHERE invoice_number IN (<invoice numbers from the file>);
-- Stamps: last ship date already covered
SELECT MAX(ship_date) FROM `americanflat.finance.stamps_shipping_costs`;
-- UPS: is this invoice already loaded?
SELECT DISTINCT invoice_number FROM `americanflat.finance.ups_shipping_costs`
WHERE invoice_number IN (<UPS invoice numbers>);
```

The UPS invoice still goes in the AP message even if it's already in BigQuery (unless Anthony
says it was in last week's message).

## Step 3 — Process

```bash
python3 <skill-dir>/scripts/process_invoices.py <files...> --out <work-dir> \
  --last-stamps-account <config.last_stamps_account> \
  --skip-fedex-invoices <already-loaded FedEx invoice numbers, comma-separated> \
  --stamps-after <MAX(ship_date) from Step 2>
```

Writes `fedex_append_<date>.csv`, `stamps_append_<date>.csv`, `ups_append_<date>.csv`, and
`summary.json`, containing this week's rows only.

Report to the user, briefly: per carrier the package/label count, total, ship-date range, and
every entry in each carrier's `flags`. Also call out:
- **UPS:** most packages are third-party billed to Amazon at $0. Say how many actually cost
  money (`paid_packages`) and the account-level fees.
- **Stamps account change:** if flagged, ask whether to update `last_stamps_account` in config.
- **Missing carriers** or **unrecognized files**.

## Step 4 — FedEx → BigQuery (sign-off required)

Use `scripts/carrier_invoices_load.py` (the same loader as `tools/carrier_invoices_load.py` on
the `claude/fedex-ups-shipping-costs-loaders` branch). Pass the **original FedEx export(s)**,
not the append file, under their real names; strip any 8-hex upload prefix
(`70fc578d-FedEx_invoice_...CSV` → `FedEx_invoice_...CSV`) by copying, so `source_file` stays
clean. Don't edit the file contents: the load log keys on the file's SHA-256.

1. Dry run (no cloud calls): checks each invoice's lines tie to its stated total.
   ```bash
   python3 <skill-dir>/scripts/carrier_invoices_load.py --dry-run <FedEx CSV(s)>
   ```
2. Show Anthony the new invoices (from Step 2) with lines and totals, and say how many rows are
   already in the table. Ask for a yes.
3. On yes, run without `--dry-run`. Don't set `CLOUDSDK_CORE_ACCOUNT` or any account override:
   the loader finds the signed-in identity itself (in a cloud session that's
   `cluade-service-account@americanflat.iam.gserviceaccount.com`, which has WRITER on
   `finance`).
4. The result line must read `<new> new, <already> already present · verified n/n rows` with
   matching dollars. Anything else (REFUSED, VERIFY FAILED, an error): stop and tell Anthony;
   don't retry with override flags.

On PERMISSION DENIED, tell him which grant is missing (BigQuery Data Editor on the `finance`
dataset for the identity the loader printed); never change IAM yourself.

The same loader handles UPS files into `finance.ups_shipping_costs` if Anthony asks.

## Step 5 — UPS charges → Yusen (South Carolina)

UPS account H9229J is South Carolina; any package on it with a charge (`paid_packages`) is a
warehouse error billed back to Yusen. Context lives in the Gmail thread "South Carolina billing
UPS account" with Leandra Murchison (Yusen), where Anthony keeps a running credit-memo total.

1. Break down the paid packages from `ups_append_<date>.csv` by ship date and reference type
   (AMS* reference / other reference / blank = missing-PLD fees), plus account-level fees.
2. Read the thread (`search_threads` for `to:leandra.murchison@us.yusen-logistics.com UPS`,
   then `get_thread`). If the invoice number is already in it with the same totals, it's been
   reported: don't draft a duplicate. Say so, and offer a short follow-up instead.
3. If it hasn't been reported, draft (Gmail `create_draft`, as a reply in that thread) an email
   in the same format as Anthony's earlier ones: what's on the invoice by ship date, the
   questions it raises, and the updated running credit-memo total. Never send it.

## Step 6 — Draft the AP message (never auto-send)

```bash
python3 <skill-dir>/scripts/build_message.py <work-dir>/summary.json [--ups-credit-note]
```

The layout (bold headers, M/D dates, bullets, the Stamps.com "no action needed" notice to tie
the Auctane invoices to) is the one Anthony approved on 2026-10-27; keep it.

Add `--ups-credit-note` when the UPS paid packages are being credited back by Yusen (Step 5).
Processing flags are internal and are left out unless `--with-notes` is passed. The message
opens with an @-mention of the AP recipient's member ID (see **Addressing people**).

Show Anthony the text, then create it as a **Slack draft** (`slack_send_message_draft`) with
`channel_id` = `config.ap_recipient.slack_user_id`, so he reviews and sends it himself. If a
draft already exists in that DM, the tool refuses; tell him to clear it. If Slack isn't
connected in this session, print the message for him to paste.

## Step 7 — Google Sheets (optional)

The Sheets connector can't append rows, so this is manual: open each sheet in `config.sheets`
→ **File → Import → Upload → Append to current sheet**. The append files' columns match each
sheet's header exactly. Note that both sheets stopped being updated on 2026-04-30; BigQuery is
the record now. Ask Anthony whether he still wants them appended before handing the files over.

## Step 8 — Wrap up

Summarize what was produced and what's still on Anthony: sending the Slack draft, any Gmail
draft, and the Sheets import if he wants it. Mention that the cost-per-unit report and
dashboard refresh still need this week's 3PL shipped-order files (NJ, Fontana, SC) and are
separate skills.

## Requirements
- Python 3.9+ (stdlib only)
- Google Cloud CLI (`bq`): signed in with `gcloud auth login` locally, or a cloud session's
  BigQuery access, for Steps 2 and 4
- Slack and Gmail connectors in Claude Code, for Steps 5 and 6 (optional; falls back to text)

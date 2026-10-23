# SOP — Weekly Shipping Cost Report

| | |
|---|---|
| **Skill** | `shipping-cost-report` |
| **Source** | ⚠ Not on GitHub. Built from the org-installed Claude skill (`shipping-cost-report`, last updated in Claude 2026-04-15). No `americanflat/skill-shipping-cost-report` repo, `ai-skills-registry` entry, or `skill-candidates` branch exists. |
| **Skill version** | ⚠ Unknown — the skill has no `skill.toml` |
| **Skill owner** | ⚠ Unknown — confirm with owner. (Anthony Armstrong is the person who receives the finished report in the companion skill `download-weekly-shipping-reports`.) · Backup: ⚠ Unknown |
| **Cadence** | Weekly. The companion skill runs the full chain on a schedule, **Thursdays 7:00 AM ET**, covering the prior Monday→Monday week. |
| **SOP generated** | 2026-10-23. If the skill is updated in Claude after this date, regenerate this SOP. |

## What this skill does

It works out what Americanflat actually paid to ship each order, and the **shipping cost per
unit (CPU) by marketplace** (Target, Shopify, Michaels, Macy's, and the rest). It does this by
matching what the carriers billed (FedEx invoices and Stamps.com labels) against what the three
Taylored 3PL warehouses (NJ, Fontana, SC) say they shipped. The result is the core weekly
shipping cost report.

**You'll need about:** 20–40 minutes, including gathering files (⚠ rough estimate, confirm with
owner) · **Output:** `shipping_cost_report.xlsx` (plus an optional 3-page PDF summary)

**Terms used below:** *3PL* = third-party logistics warehouse (Taylored). *CPU* = cost per unit
shipped. *Coverage %* = the share of shipped units that were matched to an invoice.

---

## Part A — Run it in Claude

> **Usually you don't run this skill by itself.** The companion skill
> `download-weekly-shipping-reports` downloads all five input files, runs this report, and
> posts the result to Slack. Use Part A when the files are already in hand, or when the
> automated Thursday run failed partway.

### Before you start

- [ ] Claude.ai (or Claude desktop), with the `shipping-cost-report` skill enabled in
      Settings → Skills.
- [ ] The five input files for the week. See *Inputs* in Part B for where each one comes from.

### Steps

1. Open Claude and start a new conversation.
2. Attach all five input files.
3. Type:

   ```
   Run the shipping cost report for the week of {Mon M/D} to {Mon M/D}. Attached are the FedEx
   invoice CSV, the Stamps.com print history CSV, the Fontana and New Jersey shipped order
   reports, and the South Carolina order details XLSX.
   ```

4. Claude runs the matching script, then builds the enhanced workbook. The primary report
   covers Target, Shopify, Michaels and Macy's, with an "OTHER" report for everything else.
5. If you need the presentation version, follow up with:

   ```
   Also generate the PDF visual summary.
   ```

6. **Done when** Claude gives you a summary (total matched, coverage %, blended CPU), a link to
   the Excel report, and any flagged concerns.

There are no approval gates. This skill doesn't send, post, or write anything outside the
conversation.

---

## Part B — When the owner is unavailable

### 1. Prerequisites and access

| What | Why it's needed | How to get it if you don't have it |
|---|---|---|
| `shipping-cost-report` skill enabled in Claude | Runs the matching and builds the report | Settings → Skills. If it isn't listed, ask a Governor in **#ai-github-skills** |
| Taylored portal login, client **AMF** (Fontana) and **AME** (NJ) | Fontana and NJ shipped order reports | ⚠ Unknown who grants access — confirm with owner |
| Taylored SC portal login (`tsonline2.tpservices.com`) | SC order details | ⚠ Unknown — confirm with owner |
| Stamps.com login (`print.stamps.com`) | Print history | ⚠ Unknown — confirm with owner |
| FedEx Billing Online login | FedEx invoice CSV | ⚠ Unknown — confirm with owner |

The companion skill logs in with credentials saved in **one person's Chrome browser**. If
that's the owner's machine and they're out, you need your own logins to the five portals.
Never paste passwords into Claude.

### 2. Inputs — where each one comes from

Date range: the **prior Monday → Monday**. The "To" date is inclusive. For example, on Thursday
6/18 you pull 6/8 → 6/15.

| Input | Where to find it | Format / must contain | Notes |
|---|---|---|---|
| Fontana shipped orders | `tsonline.tpservices.com` → Sales Orders → Shipped Orders, client **AMF** → green "Export to Excel" | CSV: `Order`, `PO #`, `Bill of Lading`, `Ship Date`, `Batch#`, `Units`, `Carrier`, `Consignee` | ~2.5k rows |
| New Jersey shipped orders | Same portal, switch client to **AME**. Wait for the grid to fully redraw before exporting | Same as Fontana | ~4k rows, mostly UPS `1Z…` tracking |
| South Carolina order details | `tsonline2.tpservices.com` → Sales Orders → **Order Details** → Export to Excel (all rows) | XLSX, one row per line item: `Order No.`, `Cust PO No.`, `Tracking Number`, `Division`, `Shipped Quantity`, `Actual Ship Date` | Can be large and slow to open |
| Stamps.com print history | `print.stamps.com` → History → Advanced Search → Export | CSV: `Tracking #`, `Amount Paid`, `Ship Date`, `Order ID`, `Weight` | **Type** the dates; don't pick them. Check that the grid shows the closing Monday before exporting |
| FedEx invoice | fedex.com → Billing (FBO) → **Reporting → Download center** → newest `FEDEX_INVOICE_*` | CSV: `Express or Ground Tracking ID`, `Net Charge Amount`, `Shipment Date`, reference fields | — |

**Name the files so the warehouse is detectable.** The script works out which warehouse a file
came from by its filename:
- Fontana: the name must contain `fontana`.
- New Jersey: the name must contain `nj` or `new jersey` (with a space).

A name like `NewJersey_ShippedOrders.csv` shows up as **"Unknown"** in the Warehouse columns
(tested 2026-10-23). Name it something like `NJ_ShippedOrders_2026-06-08.csv`.

If a file is missing, the skill also says to check Slack **#operations-team-information-channel**
for recent EDI reports.

### 3. Step-by-step run

**Option 1: the full automated chain (preferred if you have Claude in Chrome and the logins)**

1. In Claude with the Claude in Chrome extension connected, type:

   ```
   Run the weekly shipping reports
   ```

2. Claude logs into each portal, downloads the five files into
   `~/Documents/Claude/Projects/Weekly Shipping Reports/{prev_monday}_to_{this_monday}/`, runs
   this report, and posts to Slack **#dp-and-inventory**.
3. Jump to step 6 below to verify.

**Option 2: by hand, then Claude**

1. **Gather inputs.** Download the five files from the table above for the prior Monday → Monday.
2. **Sanity-check the inputs** before running:
   - Exactly five files.
   - Each file's dates sit inside the week.
   - Row counts are in the usual range (Fontana ~2.5k, NJ ~4k).
   - Fontana and NJ are named per the rule above.
3. **Start Claude**, attach the five files, and paste:

   ```
   Run the shipping cost report for {Mon M/D} to {Mon M/D}. Files attached: FedEx invoice,
   Stamps.com print history, Fontana + New Jersey shipped orders, South Carolina order details.
   Primary report: Target, Shopify, Michaels, Macy's. Put everything else in an OTHER report.
   Flag low-coverage marketplaces, large unmatched invoice amounts, and big week-over-week
   CPU changes.
   ```

4. **What happens next:**
   1. Claude parses the carrier files and the 3PL files.
   2. It matches them in four passes: tracking number, then PO / order reference, then two
      "reverse" passes that catch multi-package shipments.
   3. It calculates CPU by marketplace, week, and carrier.
   4. It builds the Excel report.
5. **Approval gates:** none.
6. **Verify the output.** It's good if:
   - **Coverage is 80% or higher** for each primary marketplace. Below that, see
     Troubleshooting.
   - The workbook has these tabs: **Weekly Cost per Unit**, **Weekly Volume**, **Marketplace
     Summary**, **Warehouse Summary**, **Matched Orders**, **Unmatched 3PL Orders**,
     **Unmatched Invoices**.
   - Warehouse Summary shows **Fontana, New Jersey, South Carolina**, and no "Unknown".
   - Blended CPU is in a believable range. For reference, the 6/8→6/15 week came in at
     **$8.18/unit** (Target $8.30, Shopify $6.85, Michaels $9.87, Macy's $13.76). This is one
     data point, not a threshold; ⚠ confirm the normal range with the owner.
   - No large "Unknown" marketplace bucket.
7. **Deliver it.** Post the headline numbers (blended CPU, matched count) and the file to Slack
   **#dp-and-inventory**. ⚠ Confirm with the owner whether anyone else gets it (finance?
   leadership?).

### 4. Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Claude doesn't use the skill / answers generically | Skill not enabled, or prompt too vague | Enable `shipping-cost-report` in Settings → Skills, or start the message with "Run the shipping cost report" |
| Match rate / coverage below 80% | Invoice dates don't overlap the 3PL dates, or the tracking formats are off | Re-check that all five files cover the same Monday→Monday. FedEx tracking is 12 or 15 digits; Stamps has `1Z…` (UPS) and `94…` (USPS) |
| Last week of the month shows low coverage | FedEx/Stamps invoices lag a few days | Expected. Note it in the report, or re-run once the newer FedEx invoice posts |
| A marketplace shows as "Unknown" | The 3PL `Batch#`/`Division` code isn't in the script's marketplace list | Note the raw code and tell the owner; it must be added to `MARKETPLACE_MAP` in the script |
| Warehouse shows as "Unknown" | Fontana/NJ filename doesn't contain `fontana` / `nj` | Rename the file (see Inputs) and re-run |
| SC file times out or is very slow | The SC XLSX is large | Ask Claude to load it read-only (`read_only=True, data_only=True`) |
| Stamps weights are all 0 | Weight column is in "Xlb Yoz" text | Ask Claude to use the skill's "Xlb Yoz" weight parser |
| Second portal download never lands (automated chain) | Chrome extension tab collapsed | Fully quit and reopen Chrome, then re-run |
| Stamps/Taylored dates didn't apply | Date pickers ignore pasted values | Type the dates, and check the grid shows the closing Monday before exporting |
| Output looks wrong and you can't tell why | — | Don't send it. Keep the five inputs and the output, then escalate |

### 5. Manual fallback (no Claude)

You can run the **base** report from a terminal with Python 3. It needs `openpyxl`, which the
script installs itself if it's missing:

```bash
python3 process_shipments.py \
  --fedex FedEx_Invoice.csv \
  --stamps Stamps_PrintHistory.csv \
  --nj-fontana Fontana_ShippedOrders.csv NJ_ShippedOrders.csv \
  --sc SouthCarolina_OrderDetails.xlsx \
  --output-dir ./out
```

This gives a 5-tab `shipping_cost_report.xlsx`:
- Marketplace Summary
- Warehouse Summary
- Matched Orders
- Unmatched 3PL Orders
- Unmatched Invoices

It also gives `marketplace_summary.json`, and it prints the CPU table to the screen.

**What you don't get without Claude:**
- the Weekly CPU / Weekly Volume tabs
- the primary-vs-OTHER marketplace split
- the formatting
- the PDF

Those are built by Claude from the skill's instructions, not by the script.

⚠ The script isn't in a GitHub repo. You'd have to get a copy from the skill files in Claude,
or from the owner's machine (`~/.claude/skills/shipping-cost-report/scripts/`).

### 6. Escalation

1. Skill owner: ⚠ Unknown — confirm with owner
2. Backup: ⚠ Unknown — confirm with owner
3. Skill won't load or is missing from Claude: Governors in **#ai-github-skills**

Tell them:
- what you ran, and the exact prompt
- the five input filenames and their date range
- the error, or what looks wrong in the output

---

## Reference

- **Systems touched (all read-only):** Taylored portals (Fontana, NJ, SC), Stamps.com, FedEx
  Billing Online. Slack is used only by the companion skill.
- **Bundled files:** `scripts/process_shipments.py` does the parsing, the four-pass matching
  and the base Excel report.
- **Related skills:**
  - `download-weekly-shipping-reports` (upstream) gathers the inputs and runs this report.
  - `marketplace-cpu-analysis` and `cost-per-sku-dashboard` (downstream) read the same five
    files.
- **Recent changes:** ⚠ Unknown. There's no CHANGELOG.

## Open questions for the owner

1. Who owns this skill, and who is the backup?
2. Should this skill be published to GitHub (`americanflat/skill-shipping-cost-report`) with a
   version and changelog, so changes are tracked?
3. Who grants access to the Taylored, Stamps.com and FedEx portals for a stand-in?
4. What's the normal blended CPU range, and what week-over-week change should trigger a flag?
5. Who receives the report besides #dp-and-inventory?
6. **Possible bug:** with the companion skill's staged filename `NewJersey_ShippedOrders_*.csv`,
   NJ orders come out as warehouse "Unknown". Costs and CPU are unaffected; only the
   Warehouse column and Warehouse Summary tab are wrong. Fix: also match `newjersey` in
   `detect_warehouse_name`.

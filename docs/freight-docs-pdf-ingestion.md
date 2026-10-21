# Freight docs PDF ingestion — Gmail → GCS → Cloud Run → BigQuery

Customs and freight paperwork arrives by email in the Administrator@ mailbox:
CBP 7501 entry summaries, CLA/LAX/EWR invoices and per diem bills from
Worldwide Logistics (`wwllmail.com`), bill.com approval reminders for CBP
payments, and customs release/clearance notices. This pipeline exports them
from Gmail to GCS hourly, extracts every document into a raw record, loads
those into a landing table, and sends 7501s on to `finance.customs_entries`
through the existing loader.

Only the 7501 has a known layout, so only the 7501 gets a typed parse.
Everything else lands schema-on-read — full text, tables, form fields — until
real samples exist to write parsers against.

| Part | Path |
|---|---|
| Gmail export (Apps Script) | [`apps_script/freight_docs_export/`](../apps_script/freight_docs_export/) |
| Extract service (Cloud Run) | [`services/freight_docs_extract/`](../services/freight_docs_extract/) |
| Load job (Cloud Run job) | [`services/freight_docs_load/`](../services/freight_docs_load/) |
| Deploy | [`deploy/deploy.sh`](../deploy/deploy.sh), settings [`deploy/freight_docs.env.example`](../deploy/freight_docs.env.example) |
| Landing table | `americanflat.finance.freight_documents_raw` — schema [`schemas/freight_documents_raw.json`](../schemas/freight_documents_raw.json), DDL [`schemas/freight_documents_raw.sql`](../schemas/freight_documents_raw.sql) |
| 7501 target | `americanflat.finance.customs_entries` — see [`customs-entries.md`](customs-entries.md) |

## Status

Built 2026-10-21 on branch `claude/freight-docs-pdf-ingestion` (cut from
`claude/confident-cori-l7xy56`, which holds the 7501 loader). **Nothing is
deployed and no table exists.** Nobody on the project can open the
Administrator@ mailbox, so the Gmail searches have not been run against real
mail.

Checked: `python3 -m py_compile` on both services; the existing
`tools/test_customs_entries_load.py` passes unchanged; `bash -n` and
`deploy/deploy.sh --dry-run`; the Apps Script parses (`node --check`); the
extractor was exercised locally against an in-memory GCS (unknown PDF, 7501
from the test fixture text, scanned PDF, hash mismatch, email body, replays,
a canned Document AI response). Not checked: shellcheck (not installed), the
Docker builds, anything against GCP or Gmail.

## Flow (an ASCII diagram from Gmail to BigQuery)

```
 Administrator@ Gmail
   from:wwllmail.com has:attachment                       (7501s, CLA/LAX/EWR invoices, per diem)
   from:hq.bill.com "Customs and Border Protection"       (payment reminders: body text)
   subject:("customs release" OR "customs clearance")     (entry <-> B/L / container: body text)
        |
        |  Apps Script, hourly, as Administrator@  (threads without label ops/exported)
        |  POST GCS JSON API, ifGenerationMatch=0; label ops/exported after the last write
        v
 gs://americanflat-freight-docs/raw/
   <doc_type>/<YYYY>/<MM>/<sha256>.meta.json   (written first)
   <doc_type>/<YYYY>/<MM>/<sha256>.pdf
   email/<doc_type>/<YYYY>/<MM>/<message_id>.json
        |
        |  Eventarc: google.cloud.storage.object.v1.finalized (whole bucket; service filters names)
        v
 Cloud Run service freight-docs-extract   (SA freight-docs-extract@)
   every PDF ........ pdftotext -layout, pdfinfo
   7501 ............. parse_7501() + validate()  <- imported from tools/customs_entries_load.py
   other PDFs ....... + Document AI Form Parser (OCR, tables, form fields)
   email body ....... text as-is
        |                                         \  failure
        v                                          v
 processed/<doc_type>/<YYYY>/<MM>/<sha256>.ndjson   quarantine/<sha256>.json (reason)
   + <sha256>.tariffs.csv | <sha256>.table<N>.csv
        |
        |  Cloud Scheduler :30 every hour -> Cloud Run job freight-docs-load (SA freight-docs-load@)
        v
 1. bq load --replace finance.freight_documents_raw_stage   (new sha256s only)
    MERGE -> finance.freight_documents_raw ON sha256; drop stage; invariant rows = distinct sha256
        |
 2. for each customs_7501 row, parsed, sha256 not yet in customs_entries, not quarantined:
    python3 tools/customs_entries_load.py <pdf>          (one file per call, no --replace)
        |                    \  exit 2/3 (refused)
        v                     v
 finance.customs_entries      quarantine/<sha256>.json   (never retried, never forced)
```

## GCS layout and idempotency

Bucket `gs://americanflat-freight-docs` (us-central1, uniform access, public
access prevention on).

| Object | Written by | Contents |
|---|---|---|
| `raw/<doc_type>/<YYYY>/<MM>/<sha256>.pdf` | Apps Script | The attachment bytes. Name = SHA-256 of the bytes (`Utilities.computeDigest`) |
| `raw/<doc_type>/<YYYY>/<MM>/<sha256>.meta.json` | Apps Script | Gmail message id, thread id, date, sender, subject, attachment filename, search name and query, size |
| `raw/email/<doc_type>/<YYYY>/<MM>/<message_id>.json` | Apps Script | Body-only email: headers, plain-text body (HTML stripped if no text part), attachment names, search |
| `processed/<doc_type>/<YYYY>/<MM>/<sha256>.ndjson` | extract | One raw extraction record — exactly the landing-table columns except `ingested_*` |
| `processed/…/<sha256>.tariffs.csv` | extract | 7501 only: one row per HTS row |
| `processed/…/<sha256>.table<N>.csv` | extract | One per Document AI table |
| `quarantine/<sha256>.json` | extract or load | `{sha256, stage, reason, gcs_uri, at, by}` |

`<doc_type>` is routing, chosen by the Apps Script: `customs_7501`,
`wwl_invoice`, `wwl_per_diem`, `wwl_other` (WWL, by filename/subject),
`billcom_cbp_reminder`, `customs_release`. `<YYYY>/<MM>` is the email date in
UTC. For emails, `<sha256>` in `processed/` is the hash of the exported JSON
object. A PDF whose text contains "CBP Form 7501" is treated as a 7501
wherever it was routed (`doc_type` vs `routed_doc_type` in the table).

**Every write is create-only, and re-running any stage on the same input creates nothing new:**

| Stage | Mechanism | What a repeat does |
|---|---|---|
| Apps Script → `raw/` | `ifGenerationMatch=0` on every upload; PDF named by its SHA-256 | Existing name → HTTP 412, counted as done. The same PDF in two emails is stored once (first meta wins). Label `ops/exported` is added only after every object for the thread returned 200 or 412; a failure leaves the thread unlabelled for the next run. `LockService` stops overlapping runs. |
| extract → `processed/`, `quarantine/` | Skip if `processed/…<sha256>.ndjson` or `quarantine/<sha256>.json` exists; all writes `ifGenerationMatch=0`; the NDJSON is written last as the completion marker | Eventarc's at-least-once redelivery and racing instances end in a 412 no-op. |
| load → `freight_documents_raw` | Only sha256s not already in the table are staged; MERGE `ON sha256` (insert, or update only when `extracted_at` is newer, i.e. `--full` after a re-extraction); post-load invariant `COUNT(*) = COUNT(DISTINCT sha256)` and every staged sha256 present, else exit 6 | Nothing to stage; or MERGE matches every row and changes nothing. |
| load → `customs_entries` | The loader's own rules, unchanged: same entry + same SHA-256 skipped; different PDF for a loaded entry refused (exit 3); its own post-load verify (exit 6). The job only offers it sha256s not already in `customs_entries.source_sha256` and not in `quarantine/`, one PDF per call, never `--replace`/`--allow-mismatch` | Nothing offered. A refused document is quarantined and never offered again. |

The runtime accounts hold `storage.objectCreator` + `objectViewer` only, which
cannot overwrite or remove objects, so the create-only rule is also enforced by IAM.

## Extraction choice: pdftotext/pypdf vs Document AI (a comparison table, then the decision)

| | `pdftotext -layout` (poppler) | pypdf | Document AI (Form Parser) |
|---|---|---|---|
| Existing 7501 parser | **Its required input.** `value_under()` finds box values by column position under the caption, `chunks()` splits boxes on runs of 2+ spaces, the stray-letter regex fixes a vertical caption as pdftotext lays it out | Does not reproduce pdftotext's column spacing (its layout mode is a different algorithm), so the column anchors miss | Returns reading-order text without column spacing; the parser would need rewriting — out of scope and a behaviour change |
| Scanned PDF (no text layer) | Empty text | Empty text | OCR text |
| Tables, key/value pairs | None | None | Tables (header/body cells) and form fields with confidence |
| Unknown layouts (CLA/LAX/EWR, per diem) | Text only if digitally generated | Same | Usable for discovery whether scanned or not |
| Setup | `apt-get install poppler-utils` | pip dependency | API enable, processor create, `roles/documentai.apiUser`, region `us`, per-request page/size limits |
| Determinism / cost | Deterministic, free | Deterministic, free | Model may change between versions; billed per page — immaterial at this volume |

**Decision: both, by document type.** `pdftotext -layout` runs on every PDF;
Document AI Form Parser runs additionally on every PDF that is not a 7501.
pypdf is not used.

1. **The 7501 path stays exactly as it is.** The parser depends on
   `pdftotext -layout` output, which neither pypdf nor Document AI reproduces,
   and changing the parser is out of scope. 7501s go
   `pdftotext -layout` → `parse_7501()` → `validate()`, and the load job runs
   the existing CLI on the PDF. Document AI never sees a 7501. A scanned 7501
   (no text layer) is `failed` and quarantined, as the CLI refuses it today.
2. **The other layouts are unknown and may be scans.** pdftotext returns
   nothing for a scan, which would land as an empty `failed` record — useless
   for writing the parsers later. Document AI's OCR text, tables and form
   fields are what someone will design those parsers from.
3. **Document AI costs setup and IAM, which `deploy.sh` provisions.** Its
   absence does not stop the pipeline: with `FREIGHT_DOCS_DOCAI_ENABLED=0` (or
   if the processor is unreachable for a file) the record still lands with the
   pdftotext text and the reason in `parse_error`.

## Deploying (who runs what, in order)

All from a checkout of this branch, in a shell with `gcloud`, `bq`, `curl` and
`python3` (Cloud Shell works). Claude Code sessions are read-only on BigQuery
and cannot create resources or grant IAM; none of these steps can be run from
one.

1. **Project Owner who is also OWNER of `finance` (ivan@americanflat.com)** —
   review, then provision everything:

   ```bash
   deploy/deploy.sh --dry-run
   ```

   ```bash
   deploy/deploy.sh
   ```

   Steps, each printed as it runs: APIs → service accounts → bucket + bucket
   IAM → project IAM → Artifact Registry → build both images (Cloud Build) →
   Document AI processor → extract service + invoker grant → Eventarc trigger
   → load job + invoker grant → Scheduler → `GRANT roles/bigquery.dataEditor
   ON SCHEMA finance` to the load SA. To override names or defaults, copy
   `deploy/freight_docs.env.example` to `deploy/freight_docs.env`.
2. **OWNER of `finance`** — create the landing table (paste
   `schemas/freight_documents_raw.sql` into the BigQuery console, or):

   ```bash
   bq query --project_id=americanflat --use_legacy_sql=false "$(cat schemas/freight_documents_raw.sql)"
   ```

3. **An account that can create tables in `finance`** (ivan@, or anthony@ as of
   2026-10-19) — create the 7501 table and view, if not already done:

   ```bash
   python3 tools/customs_entries_load.py --init
   ```

4. **Administrator@ account owner** — install the Apps Script (next section).
5. **Ivan** — after the first hourly export, run the load once and look:

   ```bash
   gcloud run jobs execute freight-docs-load --region=us-central1 --project=americanflat --wait
   ```

   ```bash
   gcloud storage ls -r gs://americanflat-freight-docs/quarantine/
   ```

   ```sql
   SELECT doc_type, parse_status, COUNT(*) FROM `americanflat.finance.freight_documents_raw` GROUP BY 1, 2;
   ```

To roll out code changes, commit and re-run `deploy/deploy.sh`; images are
tagged with the git short sha (`-dirty` if uncommitted).

To reload every processed file (after a re-extraction), run the job with
`--full`: `gcloud run jobs execute freight-docs-load --args=--full …`.

To re-extract one object (after fixing a bug that quarantined it), a person
first decides to take its `quarantine/<sha256>.json` out of the way — the
pipeline never does — then posts the event by hand:

```bash
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" -H "Content-Type: application/json" -d '{"bucket":"americanflat-freight-docs","name":"raw/<doc_type>/<YYYY>/<MM>/<sha256>.pdf"}' "$(gcloud run services describe freight-docs-extract --region=us-central1 --format='value(status.url)')"
```

(the caller needs `roles/run.invoker` on the service).

## Installing the Apps Script (written for the Administrator@ account owner)

This copies email attachments from your mailbox into Americanflat's Google
Cloud storage once an hour. It only reads mail that matches three searches,
and the only change it makes to your mailbox is adding the label
`ops/exported` to the threads it has copied. It never sends, deletes or moves
mail.

1. **Check the searches first.** In Gmail, paste each of these into the search
   box and confirm the results are the documents you expect (and nothing
   private):
   * `from:wwllmail.com has:attachment`
   * `from:hq.bill.com "Customs and Border Protection"`
   * `subject:("customs release" OR "customs clearance")`

   If one is wrong, tell Ivan before going further; the searches are at the
   top of `Code.gs`.
2. Signed in as Administrator@, open <https://script.google.com> → **New project**.
   Rename it `Freight docs export`.
3. **Project Settings** (gear icon) → tick **Show "appsscript.json" manifest
   file in editor**.
4. **Editor** → open `appsscript.json`, replace its contents with
   `apps_script/freight_docs_export/appsscript.json` from this repo. Open
   `Code.gs`, replace its contents with `apps_script/freight_docs_export/Code.gs`.
   Save (Ctrl/Cmd+S). The Gmail service appears under **Services** by itself.
5. Select the function **`exportFreightDocs`** in the toolbar and click **Run**.
   Google will ask you to authorize. The screen lists exactly these permissions:
   * *View and modify but not delete your email* — read the matching mail and
     add the `ops/exported` label (`gmail.modify`).
   * *Manage your data in Google Cloud Storage* — upload the files
     (`devstorage.read_write`; the bucket permission only allows creating new
     files, not changing or removing them).
   * *Connect to an external service* — the upload call itself.
   * *Allow this application to run when you are not present* — the hourly trigger.

   If Google warns that the app is unverified, choose **Advanced → Go to
   Freight docs export**; it is your own script.
6. Open **Executions** (left sidebar) and check the run: the last log line is
   like `{"threads":12,"created":30,"existed":0,"failedThreads":0,"deferred":0}`.
   `failedThreads` above 0 shows an error message above it; send that to Ivan.
   The first runs work through the backlog 50 threads per search at a time.
7. Select **`installHourlyTrigger`** and click **Run**. Running it again is
   harmless. Under **Triggers** (clock icon) you will see one hourly trigger.
8. In Gmail, the label `ops/exported` now appears on the copied threads.
   Removing that label from a thread makes it be checked again next hour;
   files already copied are not duplicated.

To stop it, disable or remove the trigger under **Triggers**. If an upload
fails with "403" mentioning the Cloud Storage API not being enabled, ask Ivan
to move the script to the `americanflat` Cloud project (Project Settings →
Google Cloud Platform project).

## IAM and access requests (a table: Principal | Role | Resource | Who grants | Why)

`deploy.sh` makes every grant marked *deploy.sh*; all are additive
(`add-iam-policy-binding`, `GRANT`). Nothing is granted to or changed on
`invoice-writer@`.

| Principal | Role | Resource | Who grants | Why |
|---|---|---|---|---|
| Deployer (ivan@americanflat.com) | Owner (or Service Usage Admin, Service Account Admin + User, Project IAM Admin, Storage Admin, Artifact Registry Admin, Cloud Build Editor, Cloud Run Admin, Eventarc Admin, Cloud Scheduler Admin, Document AI Admin) | project `americanflat` | Org/project admin (existing access to confirm) | Run `deploy/deploy.sh` |
| Deployer (ivan@americanflat.com) | Dataset OWNER | dataset `finance` | Already held | Step 12 `GRANT`, and creating `freight_documents_raw` |
| `user:administrator@americanflat.com` | `roles/storage.objectCreator` | bucket `americanflat-freight-docs` | deploy.sh | Apps Script uploads to `raw/`; cannot overwrite or remove |
| `freight-docs-extract@` | `roles/storage.objectViewer`, `roles/storage.objectCreator` | bucket | deploy.sh | Read `raw/`; write `processed/`, `quarantine/` |
| `freight-docs-extract@` | `roles/documentai.apiUser` | project | deploy.sh | Call the Form Parser processor |
| `freight-docs-load@` | `roles/storage.objectViewer`, `roles/storage.objectCreator` | bucket | deploy.sh | Read `processed/` and 7501 PDFs; write `quarantine/` |
| `freight-docs-load@` | `roles/bigquery.jobUser` | project | deploy.sh | Run load and query jobs |
| `freight-docs-load@` | `roles/bigquery.dataEditor` (WRITER) | dataset `finance` | deploy.sh step 12, as dataset OWNER | Stage table, MERGE into `freight_documents_raw`, append to `customs_entries` |
| `freight-docs-trigger@` | `roles/eventarc.eventReceiver` | project | deploy.sh | Eventarc trigger identity |
| `freight-docs-trigger@` | `roles/run.invoker` | service `freight-docs-extract` | deploy.sh | Deliver events to the private service |
| `service-<number>@gs-project-accounts` (GCS agent) | `roles/pubsub.publisher` | project | deploy.sh | GCS publishes object events for Eventarc |
| `service-<number>@gcp-sa-pubsub` (Pub/Sub agent) | `roles/iam.serviceAccountTokenCreator` | project | deploy.sh | Signed push to Cloud Run (needed on projects created before April 2021; harmless otherwise) |
| `freight-docs-scheduler@` | `roles/run.invoker` | job `freight-docs-load` | deploy.sh | Scheduler runs the job |
| Cloud Build's service account (project default) | `roles/artifactregistry.writer`, `roles/logging.logWriter` | project | Project Owner, **only if** step 6 fails with a permission error | Push images; recent projects build as the Compute default SA, which may lack these |
| Administrator@ (Workspace) | Allow the script's OAuth scopes | Workspace | Workspace super admin, only if API access controls block unverified internal apps | Authorize the Apps Script |

## Blockers before first run

1. **Ivan (project Owner, `finance` OWNER)** — run `deploy/deploy.sh --dry-run`,
   review, then `deploy/deploy.sh`. Nothing here can be provisioned from a
   Claude Code session.
2. **Ivan (`finance` OWNER)** — create the landing table:
   `bq query --project_id=americanflat --use_legacy_sql=false "$(cat schemas/freight_documents_raw.sql)"`.
   Until it exists, the load job exits 5.
3. **Ivan or Anthony** — `python3 tools/customs_entries_load.py --init`.
   Until `finance.customs_entries` exists, the load job loads the landing table
   and then exits 7.
4. **Administrator@ account owner** — check the three searches return the
   right mail, then install the Apps Script (section above). Nobody else can:
   the mailbox is not accessible to this project. Confirm the account address;
   `deploy.sh` grants bucket access to `user:administrator@americanflat.com`
   (override with `FREIGHT_DOCS_EXPORTER`).
5. **Workspace super admin, if needed** — allow the script's OAuth scopes if
   the Workspace blocks unverified internal apps.
6. **Ivan** — the 7501 loader on the source branch is not merged to `main`;
   deploy from this branch or merge both together, so the images carry the
   same `tools/customs_entries_load.py` that was reviewed.

## Gotchas

* **One bucket, one trigger.** Eventarc cannot filter direct GCS events by
  prefix, so every object written (including the extractor's own
  `processed/` and `quarantine/` writes) invokes the service. It acknowledges
  anything that is not `raw/…/<sha256>.pdf` or `raw/email/…/*.json` without
  doing work, so there is no loop, but there are ~3 extra invocations per document.
* **Meta before PDF.** The Apps Script writes `.meta.json` before the PDF
  because the PDF's arrival triggers extraction. A PDF uploaded by hand
  without a meta file still processes, with null Gmail provenance.
* **First email wins.** A PDF sent twice (or matching two searches) is stored
  and loaded once; its row carries the first message's provenance. If the two
  copies were routed to different `doc_type`s there are two NDJSONs with one
  sha256; the load keeps the typed 7501 record, else the newest.
* **Routing is a guess.** `doc_type` for WWL mail comes from the attachment
  name and subject (`7501|entry summary`, `per diem`, `invoice|inv`, else
  `wwl_other`). The extractor re-detects 7501s from the text; other
  misroutes are only a label in the landing table and can be corrected there.
* **Quarantine is final for the pipeline.** Nothing retries a quarantined
  sha256. A refused corrected 7501 (different PDF, same entry) is the expected
  case: a person runs `tools/customs_entries_load.py --replace` on it by hand,
  as `customs-entries.md` describes.
* **Load-job exit codes.** 5 = bq/gcloud error or loader infrastructure error
  (retried next hour, not quarantined); 6 = an invariant or the 7501 loader's
  verify failed (look before trusting the table); 7 = `customs_entries` missing.
  `--max-retries=0`: a failed run waits for the next schedule, it is not retried in a loop.
* **`ingested_by`.** Both the landing rows and the 7501 loader record
  `gcloud config get-value account`, which on Cloud Run should be the job's
  service account; if it prints `unknown`, the rows still load.
* **Document AI limits.** Online requests are capped in pages and size (Form
  Parser: 15 pages per request at the time of writing). An oversized file
  keeps its pdftotext record and gets the error in `parse_error`; it is not quarantined.
* **Email `<YYYY>/<MM>` is UTC.** A message sent late on the last day of a
  month in California lands under the next month.
* **Gmail label search.** The script excludes `-label:ops/exported`; if the
  Workspace renders nested labels differently, threads would be re-exported
  each hour — harmless (412s) but wasteful. Check the execution log's `existed` count.
* **Line endings.** On Windows with `core.autocrlf`, `deploy/deploy.sh` may be
  checked out with CRLF and fail in bash; run it from Cloud Shell or macOS, or
  `git checkout` with `core.autocrlf=input`.
* **Apps Script project.** The script uses its default Apps Script Cloud
  project. If GCS uploads fail with "API not enabled", switch it to the
  `americanflat` project (Project Settings), which has the Storage API on.

#!/usr/bin/env python3
"""Cloud Run service: extract each freight document landed in GCS to NDJSON.

Eventarc delivers every `google.cloud.storage.object.v1.finalized` event for
gs://$FREIGHT_DOCS_BUCKET. Only two kinds of object are acted on; everything
else (meta files, processed/, quarantine/) is acknowledged and ignored:

  raw/<doc_type>/<YYYY>/<MM>/<sha256>.pdf            -> a PDF attachment
  raw/email/<doc_type>/<YYYY>/<MM>/<message_id>.json -> a body-only email

Each produces one raw extraction record, written to
processed/<doc_type>/<YYYY>/<MM>/<sha256>.ndjson (the email's sha256 is that of
the exported JSON object), plus CSV for tabular data:

  * every PDF: `pdftotext -layout` text and page count.
  * a CBP 7501 (routed as customs_7501, or any PDF whose text says
    "CBP Form 7501"): typed parse with parse_7501() and validate(), imported
    from tools/customs_entries_load.py unchanged -> parse_status parsed, and
    <sha256>.tariffs.csv. Document AI is never used on this path.
  * any other PDF: Document AI Form Parser, when FREIGHT_DOCS_DOCAI_PROCESSOR_ID
    is set - OCR text, tables (also <sha256>.table<N>.csv), form fields ->
    parse_status unparsed. No typed parse: those layouts have no samples yet.
  * email body: the text -> parse_status unparsed.

Idempotency. The NDJSON is written last and with ifGenerationMatch=0; if it
already exists the event is a no-op, and a second writer racing the first gets
a 412 and stops. CSVs are written the same way, before it. A document that
cannot be extracted gets quarantine/<sha256>.json (the reason) and a `failed`
record, and the event is acknowledged so Eventarc does not redeliver it; an
unexpected exception is quarantined the same way, without a record. A
quarantine that names a processed_uri does not stop a retry, so a record
whose write failed after its quarantine is still written. Only transient errors
(GCS/Document AI 429 or 5xx, network) answer 500, so Eventarc retries them.

Auth: the service account's token from the metadata server; no keys.
Stdlib only, plus poppler (pdftotext, pdfinfo) in the image.
"""
import base64, csv, hashlib, io, json, os, re, subprocess, sys, tempfile, traceback
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# tools/ sits two levels up in the repo and in the image (/app/tools).
TOOLS_DIR = Path(os.environ.get("FREIGHT_DOCS_TOOLS_DIR") or Path(__file__).resolve().parents[2] / "tools")
sys.path.insert(0, str(TOOLS_DIR))
import customs_entries_load as cel  # noqa: E402  (the 7501 parser and validator, unchanged)

BUCKET = os.environ.get("FREIGHT_DOCS_BUCKET", "americanflat-freight-docs")
PROJECT = os.environ.get("FREIGHT_DOCS_PROJECT", "americanflat")
DOCAI_LOCATION = os.environ.get("FREIGHT_DOCS_DOCAI_LOCATION", "us")
DOCAI_PROCESSOR_ID = os.environ.get("FREIGHT_DOCS_DOCAI_PROCESSOR_ID", "")
VERSION = os.environ.get("FREIGHT_DOCS_VERSION", "dev")

PDF_RE = re.compile(r"^raw/(?!email/)([a-z0-9_]+)/(\d{4})/(\d{2})/([0-9a-f]{64})\.pdf$")
EMAIL_RE = re.compile(r"^raw/email/([a-z0-9_]+)/(\d{4})/(\d{2})/([^/]+)\.json$")
SEVEN501_MARKER = "CBP Form 7501"
TARIFF_COLS = ["line_number", "invoice_number", "hts_number", "description", "gross_weight_kg",
               "quantity", "quantity_uom", "entered_value", "rate", "duty"]


class Transient(Exception):
    """Retryable: answer 500 so Eventarc redelivers."""


# --------------------------------------------------------------------------- GCP plumbing

def token():
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["access_token"]


def http(method, url, body=None, headers=None, timeout=120):
    """(status, bytes). 429/5xx raise Transient; 404/412 are returned to the caller."""
    h = {"Authorization": f"Bearer {token()}"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        if e.code == 429 or e.code >= 500:
            raise Transient(f"{method} {url}: HTTP {e.code} {payload[:300]!r}")
        return e.code, payload
    except (urllib.error.URLError, TimeoutError) as e:
        raise Transient(f"{method} {url}: {e}")


def gcs_url(name):
    return (f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/"
            f"{urllib.parse.quote(name, safe='')}")


def gcs_read(name):
    status, data = http("GET", gcs_url(name) + "?alt=media")
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"read gs://{BUCKET}/{name}: HTTP {status} {data[:300]!r}")
    return data


def gcs_exists(name):
    status, data = http("GET", gcs_url(name) + "?fields=name")
    if status in (200, 404):
        return status == 200
    raise RuntimeError(f"stat gs://{BUCKET}/{name}: HTTP {status} {data[:300]!r}")


def gcs_create(name, data, content_type):
    """Create the object unless it exists. True = written, False = already there."""
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?uploadType=media"
           f"&ifGenerationMatch=0&name={urllib.parse.quote(name, safe='')}")
    status, body = http("POST", url, data, {"Content-Type": content_type})
    if status == 200:
        return True
    if status == 412:
        return False
    raise RuntimeError(f"write gs://{BUCKET}/{name}: HTTP {status} {body[:300]!r}")


# --------------------------------------------------------------------------- extraction

def page_count(pdf_path):
    try:
        out = subprocess.run(["pdfinfo", str(pdf_path)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^Pages:\s+(\d+)", out.stdout or "", re.M)
    return int(m.group(1)) if m else None


def parse_7501(text):
    """The typed 7501 record, via the existing parser and validator. Raises cel.ParseError."""
    e = cel.parse_7501(text)
    findings = cel.validate(e)
    e["validation_findings"] = findings
    e["validation_status"] = ("flagged" if any(f["severity"] in ("error", "flag") for f in findings)
                              else "ok")
    # The loader refuses an entry with error findings (exit 3, unless
    # --allow-mismatch, which the load job never passes).
    e["loader_would_refuse"] = any(f["severity"] == "error" for f in findings)
    return cel.to_json(e)


def tariff_csv(typed):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TARIFF_COLS, lineterminator="\n")
    w.writeheader()
    for ln in typed["lines"]:
        for t in ln["tariffs"]:
            w.writerow({"line_number": ln["line_number"], "invoice_number": ln["invoice_number"],
                        **{k: t.get(k) for k in TARIFF_COLS[2:]}})
    return buf.getvalue()


def anchor_text(doc_text, layout):
    segs = ((layout or {}).get("textAnchor") or {}).get("textSegments") or []
    return "".join(doc_text[int(s.get("startIndex", 0)):int(s["endIndex"])] for s in segs).strip()


def document_ai(pdf_bytes):
    """Form Parser on the PDF. Returns (docai_text, tables, entities, note)."""
    url = (f"https://{DOCAI_LOCATION}-documentai.googleapis.com/v1/projects/{PROJECT}/locations/"
           f"{DOCAI_LOCATION}/processors/{DOCAI_PROCESSOR_ID}:process")
    body = json.dumps({"rawDocument": {"content": base64.b64encode(pdf_bytes).decode(),
                                       "mimeType": "application/pdf"}}).encode()
    status, data = http("POST", url, body, {"Content-Type": "application/json"}, timeout=300)
    if status != 200:
        # 4xx is about this file (too many pages, too large, unreadable):
        # keep the pdftotext record and say why Document AI is missing.
        return None, None, None, f"Document AI HTTP {status}: {data[:300].decode(errors='replace')}"
    doc = json.loads(data).get("document", {})
    text = doc.get("text", "")
    tables, fields = [], []
    for page in doc.get("pages", []):
        n = page.get("pageNumber")
        for t in page.get("tables", []):
            tables.append({
                "page": n,
                "header_rows": [[anchor_text(text, c.get("layout")) for c in r.get("cells", [])]
                                for r in t.get("headerRows", [])],
                "body_rows": [[anchor_text(text, c.get("layout")) for c in r.get("cells", [])]
                              for r in t.get("bodyRows", [])],
            })
        for f in page.get("formFields", []):
            fields.append({"page": n, "name": anchor_text(text, f.get("fieldName")),
                           "value": anchor_text(text, f.get("fieldValue")),
                           "confidence": (f.get("fieldValue") or {}).get("confidence")})
    ents = [{"type": e.get("type"), "text": e.get("mentionText"), "confidence": e.get("confidence")}
            for e in doc.get("entities", [])]
    return text, tables, {"form_fields": fields, "entities": ents}, None


def table_csv(table):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for row in table["header_rows"] + table["body_rows"]:
        w.writerow(row)
    return buf.getvalue()


# --------------------------------------------------------------------------- handlers

def now():
    return datetime.now(timezone.utc).isoformat()


def base_record(sha, doc_type, source_kind, raw_name, processed_name):
    return {"sha256": sha, "doc_type": doc_type, "routed_doc_type": doc_type, "source_kind": source_kind,
            "parser": None, "parser_version": VERSION, "parse_status": None, "parse_error": None,
            "page_count": None, "full_text": None, "docai_text": None, "tables": None, "entities": None,
            "typed": None, "gcs_uri": f"gs://{BUCKET}/{raw_name}",
            "processed_uri": f"gs://{BUCKET}/{processed_name}", "source_file": None,
            "gmail_message_id": None, "gmail_thread_id": None, "email_date": None, "email_from": None,
            "email_subject": None, "search_query": None, "meta": None, "extracted_at": None}


def quarantine(sha, stage, reason, raw_name, processed_uri=None):
    """processed_uri: the `failed` record that follows this quarantine. Kept in
    the quarantine body so a retry knows that record may still be owed."""
    body = {"sha256": sha, "stage": stage, "reason": reason, "gcs_uri": f"gs://{BUCKET}/{raw_name}",
            "processed_uri": processed_uri, "at": now(), "by": "freight-docs-extract", "version": VERSION}
    if gcs_create(f"quarantine/{sha}.json", json.dumps(body, indent=2).encode(), "application/json"):
        print(json.dumps({"severity": "WARNING", "message": f"quarantined {sha}: {reason}"}), flush=True)


def parked(sha):
    """True when quarantine/<sha>.json is final. A quarantine that promised a
    processed record is not: if the record write failed after it (a transient
    error, then Eventarc's retry), the retry must run again and write it."""
    data = gcs_read(f"quarantine/{sha}.json")
    return data is not None and not json.loads(data).get("processed_uri")


def finish(rec, extra_files):
    """Write the CSVs, then the NDJSON. The NDJSON is the completion marker."""
    rec["extracted_at"] = now()
    for name, text in extra_files:
        gcs_create(name, text.encode(), "text/csv")
    written = gcs_create(rec["processed_uri"][len(f"gs://{BUCKET}/"):],
                         (json.dumps(rec, ensure_ascii=False) + "\n").encode(), "application/x-ndjson")
    return "processed" if written else "already processed"


def handle_pdf(name, doc_type, yyyy, mm, sha):
    stem = f"processed/{doc_type}/{yyyy}/{mm}/{sha}"
    processed = f"{stem}.ndjson"
    if gcs_exists(processed):
        return "already processed"
    if parked(sha):
        return "already quarantined"
    rec = base_record(sha, doc_type, "pdf_attachment", name, processed)

    data = gcs_read(name)
    if data is None:
        return "object gone"
    if hashlib.sha256(data).hexdigest() != sha:
        rec.update(parser="none", parse_status="failed", parse_error="content SHA-256 does not match the name")
        quarantine(sha, "extract", rec["parse_error"], name, rec["processed_uri"])
        return finish(rec, [])

    meta_raw = gcs_read(f"raw/{doc_type}/{yyyy}/{mm}/{sha}.meta.json")
    if meta_raw:
        meta = json.loads(meta_raw)
        rec.update(meta=meta, source_file=meta.get("attachment_filename"),
                   gmail_message_id=meta.get("gmail_message_id"), gmail_thread_id=meta.get("gmail_thread_id"),
                   email_date=meta.get("date"), email_from=meta.get("sender"),
                   email_subject=meta.get("subject"), search_query=meta.get("search_query"))

    with tempfile.TemporaryDirectory(prefix="freight-docs-") as tmp:
        pdf = Path(tmp) / f"{sha}.pdf"
        pdf.write_bytes(data)
        rec["page_count"] = page_count(pdf)
        try:
            rec["full_text"] = cel.extract_text(pdf)  # pdftotext -layout, as the 7501 parser expects
            text_error = None
        except cel.ParseError as exc:
            text_error = str(exc)  # e.g. a scan with no text layer
    if rec["page_count"] is None and rec["full_text"]:
        rec["page_count"] = rec["full_text"].count("\f") or None  # pdftotext ends each page with \f

    extra = []
    is_7501 = doc_type == "customs_7501" or SEVEN501_MARKER in (rec["full_text"] or "")
    if is_7501:
        rec.update(doc_type="customs_7501", parser="pdftotext-layout+customs_7501")
        try:
            if text_error:
                raise cel.ParseError(text_error)
            rec["typed"] = parse_7501(rec["full_text"])
            rec["parse_status"] = "parsed"
            extra.append((f"{stem}.tariffs.csv", tariff_csv(rec["typed"])))
        except cel.ParseError as exc:
            rec.update(parse_status="failed", parse_error=f"7501 parse: {exc}")
            quarantine(sha, "extract", rec["parse_error"], name, rec["processed_uri"])
        return finish(rec, extra)

    rec["parser"] = "pdftotext-layout"
    notes = [text_error] if text_error else []
    if DOCAI_PROCESSOR_ID:
        text, tables, entities, note = document_ai(data)
        if note:
            notes.append(note)
        else:
            rec.update(parser="pdftotext-layout+documentai-form-parser", docai_text=text, tables=tables,
                       entities=entities)
            extra += [(f"{stem}.table{i + 1}.csv", table_csv(t)) for i, t in enumerate(tables)]
    has_text = bool((rec["full_text"] or "").strip() or (rec["docai_text"] or "").strip())
    rec["parse_error"] = "; ".join(notes) or None
    if has_text:
        rec["parse_status"] = "unparsed"
    else:
        rec["parse_status"] = "failed"
        quarantine(sha, "extract", rec["parse_error"] or "no text extracted", name, rec["processed_uri"])
    return finish(rec, extra)


def handle_email(name, doc_type, yyyy, mm, message_id):
    data = gcs_read(name)
    if data is None:
        return "object gone"
    sha = hashlib.sha256(data).hexdigest()
    processed = f"processed/{doc_type}/{yyyy}/{mm}/{sha}.ndjson"
    if gcs_exists(processed):
        return "already processed"
    if parked(sha):
        return "already quarantined"
    rec = base_record(sha, doc_type, "email_body", name, processed)
    try:
        env = json.loads(data)
    except ValueError as exc:
        rec.update(parser="email-body", parse_status="failed", parse_error=f"not JSON: {exc}",
                   source_file=message_id)
        quarantine(sha, "extract", rec["parse_error"], name, rec["processed_uri"])
        return finish(rec, [])
    rec.update(parser="email-body", meta=env, source_file=message_id, full_text=env.get("body_text"),
               gmail_message_id=env.get("gmail_message_id") or message_id,
               gmail_thread_id=env.get("gmail_thread_id"), email_date=env.get("date"),
               email_from=env.get("sender"), email_subject=env.get("subject"),
               search_query=env.get("search_query"),
               parse_status="unparsed" if (env.get("body_text") or "").strip() else "failed")
    if rec["parse_status"] == "failed":
        rec["parse_error"] = "empty body"
        quarantine(sha, "extract", rec["parse_error"], name, rec["processed_uri"])
    return finish(rec, [])


def handle(name):
    m = PDF_RE.match(name)
    if m:
        return handle_pdf(name, *m.groups())
    m = EMAIL_RE.match(name)
    if m:
        return handle_email(name, *m.groups())
    return "ignored"


def quarantine_unexpected(name, detail):
    """A bug or a malformed object: park it rather than let Eventarc redeliver
    it for days. The PDF's sha256 is its name; an email's is its content."""
    m = PDF_RE.match(name)
    if m:
        sha = m.group(4)
    else:
        data = gcs_read(name)
        if data is None:
            return
        sha = hashlib.sha256(data).hexdigest()
    quarantine(sha, "extract", f"unexpected error: {detail[-1500:]}", name)


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            event = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.reply(400, "body is not JSON")
        bucket, name = event.get("bucket"), event.get("name") or ""
        if bucket and bucket != BUCKET:
            return self.reply(200, f"ignored: bucket {bucket}")
        try:
            result = handle(name)
        except Transient as exc:
            print(json.dumps({"severity": "WARNING", "message": f"retry {name}: {exc}"}), flush=True)
            return self.reply(500, "transient")
        except Exception:  # noqa: BLE001 - one bad object must not crash the server
            detail = traceback.format_exc()
            print(json.dumps({"severity": "ERROR", "message": f"{name}: {detail}"}), flush=True)
            try:
                quarantine_unexpected(name, detail)
            except Exception:  # noqa: BLE001 - cannot even quarantine: let Eventarc retry
                return self.reply(500, "error")
            return self.reply(200, "quarantined")
        print(json.dumps({"severity": "INFO", "message": f"{name}: {result}"}), flush=True)
        return self.reply(200, result)

    def do_GET(self):
        return self.reply(200, "ok")

    def reply(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

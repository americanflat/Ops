#!/usr/bin/env python3
"""Load CBP Form 7501 entry summaries into `americanflat.finance.customs_entries`.

One row per customs entry, with its lines, HTS rows, fees and the commercial
invoice reconciliation nested inside. The customs broker (Worldwide Logistics
CHB) sends the 7501 as the duty bill, so the box 44 total is what Americanflat
owes for the entry. `finance.customs_entry_tariffs` is a flat view of it.

Every entry is validated before it is written:

  * error - the form's own arithmetic does not hold (a line's duty is not
    rate x value, the duties do not sum to box 41, fees do not sum to box 43,
    41+42+43 is not box 44). On a CBP-generated form this almost always means
    the parse went wrong, so the entry is refused (override: --allow-mismatch).
  * flag  - the form adds up, but the entry looks wrong: line values do not
    sum to the declared total entered value, the duty the commercial invoice
    deducted does not match the duty actually paid, freight charges do not
    match the invoice's freight deduction. Flagged entries DO load, with
    validation_status = 'flagged' and the findings stored on the row - they
    are exactly what someone needs to query and chase with the broker.
  * info  - something could not be checked (e.g. no MPF min/max on file for
    the entry's fiscal year).

Re-running is safe: an entry already loaded from the same PDF (same SHA-256)
is skipped. A different PDF for an entry already loaded - a broker's post
summary correction, say - is refused unless --replace, which swaps the row.

Auth: uses whatever account `gcloud auth login` is signed in as (no service
account hop), like tools/carrier_invoices_load.py. Writes nothing with
--dry-run. Needs `pdftotext` (poppler: `brew install poppler`) to read PDFs;
already-extracted .txt files are accepted as-is.

Usage:
  python3 tools/customs_entries_load.py --dry-run FILE [FILE ...]   # parse + validate only
  python3 tools/customs_entries_load.py --init                      # one-time: create table + view
  python3 tools/customs_entries_load.py FILE [FILE ...]             # load

Exit codes: 0 ok · 2 bad usage / unreadable file · 3 validation error or entry conflict ·
            4 bq/gcloud/pdftotext missing · 5 BigQuery error · 6 post-load verification failed
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE.parent / "schemas" / "customs_entries.json"
VIEW_SQL = HERE.parent / "sql" / "finance_customs_entry_tariffs.sql"
PROJECT, DATASET, TABLE, VIEW = "americanflat", "finance", "customs_entries", "customs_entry_tariffs"
LOCATION = "US"

CENT = Decimal("0.01")

# MPF (class 499) is ad valorem per line but clamped per entry to a minimum and
# maximum that CBP resets every fiscal year (Oct 1 - Sep 30), published in the
# Federal Register each summer. Add the next year here when it is published;
# until then entries in that year get an `info` finding instead of a check.
MPF_LIMITS = {
    2025: (Decimal("32.71"), Decimal("634.62")),
    2026: (Decimal("33.58"), Decimal("651.50")),
}

MONEY = r"\$?\d[\d,]*\.\d{2}"
ENTRY_NO_RE = re.compile(r"\b([A-Z0-9]{3})-(\d{7})-(\d)\b")
DATE_RE = r"\d{2}/\d{2}/\d{2}(?:\d{2})?"
HTS_RE = re.compile(r"^\s*(\d{4}\.\d{2}(?:\.\d{2,4})?)\b(.*)$")
LINE_HEAD_RE = re.compile(r"^\s{0,8}(\d{3})\s+(\S.*)$")
LINE_FEE_RE = re.compile(r"^\s*(\d{3})\s+(.+?)\s{2,}(\d+(?:\.\d+)?%)\s+(" + MONEY + r")\s*$")
SUMMARY_FEE_RE = re.compile(r"^\s*(\d{3})\s+([A-Z][A-Za-z ]+?)\s+\$(\d[\d,]*\.\d{2})")
CHARGES_RE = re.compile(r"^\s*C(\d[\d,]*)\s*$")
INVOICE_NO_RE = re.compile(r"Invoice Number\s+(\S+)")
# The "Dollars / Cents" column heading of box 38 is printed vertically, so its
# letters land one per text line, mid-row ("12.5% n   191.75"). Drop a lone
# letter that has a wide gap after it, or ends the line after a wide gap.
STRAY_LETTER_RE = re.compile(r"(?:(?<=\s)|^)[A-Za-z](?=\s{2,}\S)|(?<=\s\s)[A-Za-z](?=\s*$)")


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def money(text):
    if text is None:
        return None
    t = str(text).strip().replace("$", "").replace(",", "")
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_date(text):
    t = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def cents(d):
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def chunks(line):
    """Split a layout line on runs of 2+ spaces - how pdftotext separates boxes."""
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def value_under(lines, label, start=0, max_down=4, pattern=MONEY, left=12, right=60):
    """The `pattern` match printed under `label`: on the first of the next few
    lines that has one within the label's column, the rightmost such match.
    7501 boxes put the value below the caption, so position is the only
    reliable anchor - and box 39's value shares its line with the MPF amount
    printed just left of it."""
    for i in range(start, len(lines)):
        col = lines[i].find(label)
        if col < 0:
            continue
        for j in range(i + 1, min(i + 1 + max_down, len(lines))):
            hits = [m.group(0) for m in re.finditer(pattern, lines[j])
                    if col - left <= m.start() <= col + len(label) + right]
            if hits:
                return hits[-1]
        return None
    return None


def row_after(lines, label):
    """Box chunks of the first non-blank line after the caption line `label`."""
    for i, line in enumerate(lines):
        if label in line:
            for nxt in lines[i + 1:i + 4]:
                if nxt.strip():
                    return chunks(nxt)
            return []
    return []


# --------------------------------------------------------------------------- text extraction

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8")
    exe = shutil.which("pdftotext")
    if not exe:
        print("ERROR: pdftotext not found. Install poppler (macOS: `brew install poppler`).",
              file=sys.stderr)
        sys.exit(4)
    out = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, text=True)
    if out.returncode != 0:
        raise ParseError(f"pdftotext failed: {out.stderr.strip()}")
    if not out.stdout.strip():
        raise ParseError("no text layer - this PDF is a scan and needs OCR first")
    return out.stdout


# --------------------------------------------------------------------------- parsing

def parse_header(lines, text):
    e = {}
    m = ENTRY_NO_RE.search(text)
    if not m:
        raise ParseError("no entry number (box 1) found - is this a CBP Form 7501?")
    e["entry_number"] = m.group(0)
    e["filer_code"] = m.group(1)

    box1 = re.search(
        re.escape(m.group(0)) + r"\s+(\d{2})\s+(\S+)\s+(" + DATE_RE + r")\s+(\S+)\s+(\S+)\s+(\d{4})\s+("
        + DATE_RE + r")", text)
    if box1:
        e["entry_type"], e["entry_type_detail"] = box1.group(1), box1.group(2)
        e["summary_date"] = parse_date(box1.group(3))
        e["surety_number"], e["bond_type"], e["port_code"] = box1.group(4), box1.group(5), box1.group(6)
        e["entry_date"] = parse_date(box1.group(7))

    row = row_after(lines, "8. Importing Carrier")
    if len(row) >= 4:
        e["importing_carrier"], e["mode_of_transport"], e["country_of_origin"] = row[0], row[1], row[2]
        e["import_date"] = parse_date(row[3])
    row = row_after(lines, "12. B/L or AWB Number")
    if len(row) >= 4:
        e["bill_of_lading"], e["manufacturer_id"], e["exporting_country"] = row[0], row[1], row[2]
        e["export_date"] = parse_date(row[3])
    row = row_after(lines, "19. Foreign Port of Lading")
    if len(row) >= 2:
        e["foreign_port_of_lading"], e["us_port_of_unlading"] = row[-2], row[-1]

    # Box 25 wraps onto a second line ("W185/MAERSK SEALAND - APM" / "TERMINALS").
    # Box 26's usual value, SAME (consignee = importer), is drawn one space
    # after the first line, inside box 25's columns, so it is dropped by name.
    for i, line in enumerate(lines):
        if "25. Location of Goods" in line:
            col = line.find("25.")
            end = line.find("26.") if "26." in line else col + 45
            parts = []
            for nxt in lines[i + 1:i + 3]:
                seg = nxt[col:end].strip() if len(nxt) > col else ""
                seg = re.split(r"\s{2,}", seg)[0] if seg else ""
                seg = re.sub(r"\s+SAME$", "", seg)
                if seg:
                    parts.append(seg)
            e["location_of_goods"] = " ".join(parts) or None
            break

    m = re.search(r"\b\d{2}-\d{9}[A-Z0-9]{0,2}\b", text)
    e["importer_number"] = m.group(0) if m else None
    row = row_after(lines, "30. Importer of Record")
    e["importer_name"] = row[-1] if row else None

    for i, line in enumerate(lines):
        if "46. Broker/Filer Information" in line:
            following = [l for l in lines[i + 1:i + 5] if l.strip()]
            if following:
                e["broker_name"] = chunks(following[0])[0]
            ref = next((l for l in following if "/" in l or "Ref:" in l), "")
            fm = re.search(r"(\S+)\s*/\s*Ref:", ref)
            e["broker_file_number"] = fm.group(1) if fm else (chunks(ref)[-1] if ref else None)
            pm = re.search(r"PO#\s*([\w,\- ]+)", ref)
            e["po_numbers"] = [p.strip() for p in pm.group(1).split(",") if p.strip()] if pm else []
            break
    return e


def parse_totals(lines, e):
    tev = value_under(lines, "39. Total Entered Value")
    e["total_entered_value"] = money(tev)
    for box, key in (("41. Duty", "duty"), ("42. Tax", "tax"), ("43. Other", "other_fees"),
                     ("44. Total", "total")):
        e[key] = money(value_under(lines, box, left=4, right=40))

    fees, in_summary = [], False
    for line in lines:
        if "Other Fee Summary" in line:
            in_summary = True
            continue
        if in_summary:
            m = SUMMARY_FEE_RE.match(line)
            if m:
                fees.append({"code": m.group(1), "name": m.group(2).strip(), "amount": money(m.group(3))})
            elif fees and "DECLARATION" in line:
                break
    e["fees"] = fees


def parse_lines(lines):
    """Walk the box 31-38 body of both pages. Returns the entry lines."""
    out, cur, pending_desc, invoice_no, skipping = [], None, None, None, True
    for raw in lines:
        if "Other Fee Summary" in raw or "CBP Form 7501" in raw:
            skipping = True
            continue
        if re.search(r"BILL QTY|ENTRY SUMMARY CONTINUATION SHEET", raw) or re.match(r"^\s*No\.\s", raw):
            skipping = False  # body starts after the column captions
            continue
        if skipping or "Invoice Value" in raw or "(-)" in raw or "Total Entered Value" in raw:
            continue
        line = STRAY_LETTER_RE.sub(" ", raw).rstrip()
        if not line.strip():
            continue

        m = INVOICE_NO_RE.search(line)
        if m:
            invoice_no = m.group(1)
            continue
        m = LINE_FEE_RE.match(line)
        if m:
            if cur is None:
                raise ParseError(f"fee line before any entry line: {line.strip()}")
            cur["line_fees"].append({"code": m.group(1), "name": m.group(2).strip(),
                                     "rate": m.group(3), "amount": money(m.group(4))})
            continue
        m = CHARGES_RE.match(line)
        if m and cur is not None:
            cur["charges"] = (cur["charges"] or Decimal(0)) + money(m.group(1) + ".00")
            continue
        m = HTS_RE.match(line)
        if m and cur is not None:
            cur["tariffs"].append(parse_tariff(m.group(1), m.group(2), pending_desc))
            pending_desc = None
            continue
        m = LINE_HEAD_RE.match(line)
        if m and "$" not in line and "%" not in line:
            cur = {"line_number": int(m.group(1)), "description": None, "invoice_number": invoice_no,
                   "entered_value": None, "charges": None, "tariffs": [], "line_fees": []}
            out.append(cur)
            pending_desc = chunks(m.group(2))[0]
            continue
        if cur is not None and re.search(r"[A-Za-z]{3}", line) and not re.search(r"\d+\s*KG\b", line):
            pending_desc = chunks(line)[0]

    for ln in out:
        values = [t["entered_value"] for t in ln["tariffs"] if t["entered_value"] is not None]
        ln["entered_value"] = sum(values, Decimal(0)) if values else None
        commodity = [t for t in ln["tariffs"] if not t["hts_number"].startswith("99")]
        ln["description"] = (commodity[-1]["description"] if commodity and commodity[-1]["description"]
                             else (ln["tariffs"][0]["description"] if ln["tariffs"] else None))
    return out


def parse_tariff(hts, rest, desc):
    t = {"hts_number": hts, "description": desc, "gross_weight_kg": None, "quantity": None,
         "quantity_uom": None, "entered_value": None, "rate": None, "duty": None}
    tokens = rest.split()
    if len(tokens) >= 2 and re.fullmatch(MONEY, tokens[-1]):
        t["duty"] = money(tokens[-1])
        t["rate"] = tokens[-2]
        tokens = tokens[:-2]
        if tokens and re.fullmatch(r"\d[\d,]*", tokens[-1]):
            t["entered_value"] = money(tokens[-1])
            tokens = tokens[:-1]
    i = 0
    while i < len(tokens):
        num = money(tokens[i])
        uom = tokens[i + 1] if i + 1 < len(tokens) and re.fullmatch(r"[A-Z]{1,4}", tokens[i + 1]) else None
        if num is not None and uom == "KG":
            t["gross_weight_kg"] = num
            i += 2
        elif num is not None:
            t["quantity"], t["quantity_uom"] = num, uom
            i += 2 if uom else 1
        else:
            i += 1
    return t


def parse_commercial_invoices(lines):
    """The reconciliation block follows the invoice number it belongs to."""
    invoices, cur, last_number = [], None, None
    for line in lines:
        m = re.search(r"Invoice Value\s+\w*\s+(" + MONEY + ")", line)
        if m:
            cur = {"invoice_number": last_number, "invoice_value": money(m.group(1)),
                   "freight_deduction": None, "duty_deduction": None, "other_deductions": None,
                   "entered_value": None}
            invoices.append(cur)
            continue
        if cur is None:
            n = INVOICE_NO_RE.search(line)
            if n:
                last_number = n.group(1)
            continue
        m = re.search(r"\(-\)\s+(.+?)\s{2,}(" + MONEY + ")", line)
        if m:
            name, amt = m.group(1).lower(), money(m.group(2))
            key = ("freight_deduction" if "freight" in name else
                   "duty_deduction" if "duty" in name or "tax" in name else "other_deductions")
            cur[key] = (cur[key] or Decimal(0)) + amt
            continue
        m = re.search(r"Total Entered Value \(Invoice\)\s+(" + MONEY + ")", line)
        if m:
            cur["entered_value"] = money(m.group(1))
            cur = None
    return invoices


def parse_7501(text):
    lines = text.replace("\f", "\n").splitlines()
    e = parse_header(lines, text)
    parse_totals(lines, e)
    e["lines"] = parse_lines(lines)
    e["commercial_invoices"] = parse_commercial_invoices(lines)
    if not e["lines"]:
        raise ParseError("no entry lines found")
    return e


# --------------------------------------------------------------------------- validation

def validate(e):
    """Return findings: dicts of check_name, severity, message, expected, actual."""
    found = []

    def add(check, severity, message, expected=None, actual=None):
        found.append({"check_name": check, "severity": severity, "message": message,
                      "expected": expected, "actual": actual})

    for key, box in (("total_entered_value", "39"), ("duty", "41"), ("other_fees", "43"), ("total", "44")):
        if e.get(key) is None:
            add("box_read", "error", f"box {box} ({key}) could not be read")

    # Each HTS row's duty is its rate times its value, rounded to the cent.
    # Chapter 99 rows (Section 301, IEEPA, 232...) print a value of 0: their
    # duty is charged on the value reported on the line's commodity row.
    duty_sum = Decimal(0)
    for ln in e["lines"]:
        for t in ln["tariffs"]:
            if t["duty"] is not None:
                duty_sum += t["duty"]
            rate = (t["rate"] or "").lower()
            base = t["entered_value"] or ln["entered_value"]
            if base is None or t["duty"] is None:
                continue
            if rate == "free":
                want = Decimal(0)
            elif rate.endswith("%"):
                want = cents(base * Decimal(rate[:-1]) / 100)
            else:
                add("tariff_duty", "info", f"line {ln['line_number']} {t['hts_number']}: specific rate "
                    f"{t['rate']} not checked")
                continue
            if abs(want - t["duty"]) > CENT:
                add("tariff_duty", "error", f"line {ln['line_number']} {t['hts_number']}: {t['rate']} of "
                    f"{base} is {want}, form shows {t['duty']}", want, t["duty"])
        for f in ln["line_fees"]:
            if ln["entered_value"] is not None and f["rate"].endswith("%"):
                want = cents(ln["entered_value"] * Decimal(f["rate"][:-1]) / 100)
                if abs(want - f["amount"]) > CENT:
                    add("line_fee", "error", f"line {ln['line_number']} fee {f['code']}: {f['rate']} of "
                        f"{ln['entered_value']} is {want}, form shows {f['amount']}", want, f["amount"])

    if e.get("duty") is not None and duty_sum != e["duty"]:
        add("duty_total", "error", f"line duties sum to {duty_sum}, box 41 says {e['duty']}",
            duty_sum, e["duty"])

    by_code = {}
    for ln in e["lines"]:
        for f in ln["line_fees"]:
            by_code[f["code"]] = by_code.get(f["code"], Decimal(0)) + f["amount"]
    summary = {f["code"]: f["amount"] for f in e["fees"]}
    for code, line_total in sorted(by_code.items()):
        charged = summary.get(code)
        if charged is None:
            add("fee_summary", "error", f"fee {code} on the lines but not in the Other Fee Summary")
            continue
        want = line_total
        if code == "499":
            fy = None
            if e.get("entry_date"):
                d = e["entry_date"]
                fy = d.year + 1 if d.month >= 10 else d.year
            limits = MPF_LIMITS.get(fy)
            if limits:
                want = min(max(line_total, limits[0]), limits[1])
            else:
                add("mpf_limits", "info", f"no MPF min/max on file for FY{fy}; MPF not checked")
                continue
        if want != charged:
            add("fee_summary", "error", f"fee {code}: lines give {line_total}, expected {want} charged, "
                f"summary shows {charged}", want, charged)

    if e["fees"] and e.get("other_fees") is not None:
        fee_sum = sum((f["amount"] for f in e["fees"]), Decimal(0))
        if fee_sum != e["other_fees"]:
            add("other_total", "error", f"fee summary sums to {fee_sum}, box 43 says {e['other_fees']}",
                fee_sum, e["other_fees"])
    if None not in (e.get("duty"), e.get("other_fees"), e.get("total")):
        want = e["duty"] + (e.get("tax") or 0) + e["other_fees"]
        if want != e["total"]:
            add("grand_total", "error", f"41+42+43 = {want}, box 44 says {e['total']}", want, e["total"])

    # From here on the form adds up; these ask whether the entry is right.
    values = [ln["entered_value"] for ln in e["lines"] if ln["entered_value"] is not None]
    line_value = sum(values, Decimal(0))
    tev = e.get("total_entered_value")
    if tev is not None and abs(line_value - tev) > len(e["lines"]):  # lines round to whole dollars
        msg = (f"line entered values sum to {line_value:,.2f} but box 39 declares {tev:,.2f} "
               f"(difference {tev - line_value:,.2f})")
        if line_value and e.get("duty") is not None:
            est = cents(e["duty"] * tev / line_value)
            msg += (f"; duty was assessed on the line values - at the same rates on {tev:,.2f} it "
                    f"would be about {est:,.2f}, not {e['duty']:,.2f}")
        add("entered_value_total", "flag", msg, tev, line_value)

    for inv in e["commercial_invoices"]:
        label = f"commercial invoice {inv['invoice_number'] or ''}".strip()
        if inv["entered_value"] is not None and tev is not None and abs(inv["entered_value"] - tev) > 1:
            add("invoice_entered_value", "flag", f"{label}: reconciles to {inv['entered_value']:,.2f}, "
                f"box 39 declares {tev:,.2f}", tev, inv["entered_value"])
        if inv["duty_deduction"] is not None and e.get("total") is not None \
                and abs(inv["duty_deduction"] - e["total"]) > 1:
            add("duty_deduction", "flag", f"{label}: deducts {inv['duty_deduction']:,.2f} of duty, fees "
                f"and taxes, but this entry charges {e['total']:,.2f}", inv["duty_deduction"], e["total"])
        charges = [ln["charges"] for ln in e["lines"] if ln["charges"] is not None]
        if inv["freight_deduction"] is not None and charges \
                and abs(sum(charges, Decimal(0)) - inv["freight_deduction"]) > len(charges):
            add("freight_charges", "flag", f"{label}: deducts {inv['freight_deduction']:,.2f} freight, line "
                f"charges sum to {sum(charges, Decimal(0)):,.2f}", inv["freight_deduction"],
                sum(charges, Decimal(0)))
    return found


# --------------------------------------------------------------------------- reporting

def report(e, findings, name):
    print(f"\n{name}")
    print(f"  entry {e['entry_number']}  entered {e.get('entry_date')}  B/L {e.get('bill_of_lading')}  "
          f"broker {e.get('broker_name')} ({e.get('broker_file_number')})")
    if e.get("po_numbers"):
        print(f"  POs {', '.join(e['po_numbers'])}")
    print(f"  {'line':<5}{'HTS':<14}{'value':>10}  {'rate':>8}{'duty':>10}  description")
    for ln in e["lines"]:
        for t in ln["tariffs"]:
            print(f"  {ln['line_number']:03d}  {t['hts_number']:<14}{t['entered_value'] or 0:>10,}  "
                  f"{t['rate'] or '':>8}{t['duty'] or 0:>10,.2f}  {(t['description'] or '')[:40]}")
    for f in e["fees"]:
        print(f"  fee {f['code']} {f['name']:<32}{f['amount']:>10,.2f}")
    print(f"  entered value (box 39) {e.get('total_entered_value') or 0:>12,.2f}")
    print(f"  duty {e.get('duty')}  tax {e.get('tax')}  other {e.get('other_fees')}  "
          f"TOTAL OWED (box 44) {e.get('total')}")
    if not findings:
        print("  checks: all passed")
    for f in findings:
        print(f"  {f['severity'].upper():<5} {f['check_name']}: {f['message']}")


# --------------------------------------------------------------------------- BigQuery

def bq_run(args, check=True):
    cmd = [shutil.which("bq") or "bq", f"--project_id={PROJECT}", f"--location={LOCATION}"] + args
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(f"ERROR: bq {' '.join(args[:2])} failed:\n{(r.stderr or r.stdout).strip()}", file=sys.stderr)
        sys.exit(5)
    return r


def bq_query(sql):
    r = bq_run(["query", "--use_legacy_sql=false", "--format=json", "--max_rows=10000", sql])
    out = (r.stdout or "").strip()
    return json.loads(out) if out.startswith("[") else []


def ensure_tools():
    missing = [t for t in ("bq", "gcloud") if not shutil.which(t)]
    if missing:
        print(f"ERROR: Google Cloud SDK not found (missing {', '.join(missing)}). "
              f"--dry-run works without it.", file=sys.stderr)
        sys.exit(4)


def gcloud_account():
    try:
        r = subprocess.run([shutil.which("gcloud") or "gcloud", "config", "get-value", "account"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def init():
    if bq_run(["show", "--format=none", f"{DATASET}.{TABLE}"], check=False).returncode == 0:
        print(f"{DATASET}.{TABLE} already exists")
    else:
        bq_run(["mk", "--table", "--time_partitioning_field=entry_date", "--time_partitioning_type=MONTH",
                "--clustering_fields=entry_number,bill_of_lading",
                "--description=CBP Form 7501 customs entries - one row per entry. "
                "Loader: tools/customs_entries_load.py",
                f"{PROJECT}:{DATASET}.{TABLE}", str(SCHEMA_PATH)])
        print(f"created {DATASET}.{TABLE}")
    # Drop the file's comment header: bq reads a leading "--" as a flag.
    view_sql = "\n".join(l for l in VIEW_SQL.read_text().splitlines() if not l.startswith("--"))
    bq_run(["query", "--use_legacy_sql=false", view_sql])
    print(f"created/updated view {DATASET}.{VIEW}")


def to_json(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: to_json(x) for k, x in v.items()}
    if isinstance(v, list):
        return [to_json(x) for x in v]
    return v


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="parse and validate only; touch nothing")
    ap.add_argument("--init", action="store_true", help="create the table and view if missing")
    ap.add_argument("--allow-mismatch", action="store_true",
                    help="load entries whose own arithmetic fails (errors). Flags never block")
    ap.add_argument("--replace", action="store_true",
                    help="replace an entry already loaded from a different PDF (e.g. a corrected 7501)")
    a = ap.parse_args()
    if not a.files and not a.init:
        ap.error("give at least one file, or --init")

    if not a.dry_run:
        ensure_tools()
    if a.init:
        if a.dry_run:
            ap.error("--init writes; drop --dry-run")
        init()
        if not a.files:
            return 0

    entries, refused = [], []
    for path in a.files:
        try:
            raw = path.read_bytes()
            text = extract_text(path)
            e = parse_7501(text)
        except (OSError, ParseError) as exc:
            print(f"\n{path.name}: cannot read as a 7501 - {exc}", file=sys.stderr)
            return 2
        findings = validate(e)
        report(e, findings, path.name)
        e["validation_findings"] = findings
        e["validation_status"] = "flagged" if any(f["severity"] in ("error", "flag") for f in findings) else "ok"
        e["raw_text"] = text
        e["source_file"] = path.name
        e["source_sha256"] = hashlib.sha256(raw).hexdigest()
        if any(f["severity"] == "error" for f in findings) and not a.allow_mismatch:
            refused.append(path.name)
        entries.append(e)

    numbers = [e["entry_number"] for e in entries]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    if dupes:
        print(f"\nERROR: same entry in more than one file: {', '.join(sorted(dupes))}", file=sys.stderr)
        return 3
    if refused:
        print(f"\nREFUSED (form arithmetic does not hold - check the parse, or --allow-mismatch): "
              f"{', '.join(refused)}", file=sys.stderr)
        return 3
    if a.dry_run:
        print(f"\ndry run: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} parsed, nothing written")
        return 0

    in_list = ", ".join(f"'{n}'" for n in numbers)
    existing = {r["entry_number"]: r["source_sha256"] for r in bq_query(
        f"SELECT entry_number, source_sha256 FROM `{PROJECT}.{DATASET}.{TABLE}` "
        f"WHERE entry_number IN ({in_list})")}
    to_load, conflicts = [], []
    for e in entries:
        have = existing.get(e["entry_number"])
        if have == e["source_sha256"]:
            print(f"skip {e['entry_number']}: already loaded from this PDF")
        elif have and not a.replace:
            conflicts.append(e["entry_number"])
        else:
            to_load.append(e)
    if conflicts:
        print(f"\nREFUSED: already loaded from a different PDF: {', '.join(conflicts)}. If this is a "
              f"corrected 7501, re-run with --replace.", file=sys.stderr)
        return 3
    if not to_load:
        return 0

    run_ts = datetime.now(timezone.utc).isoformat()
    who = gcloud_account()
    tmp = Path(tempfile.mkdtemp(prefix="customs-load-")) / "rows.jsonl"
    with tmp.open("w", encoding="utf-8") as fh:
        for e in to_load:
            e["ingested_at"], e["ingested_by"] = run_ts, who
            fh.write(json.dumps(to_json(e)) + "\n")

    # Append first, then drop the superseded rows: a failed load deletes nothing.
    bq_run(["load", "--source_format=NEWLINE_DELIMITED_JSON", f"{DATASET}.{TABLE}", str(tmp)])
    load_list = ", ".join(f"'{e['entry_number']}'" for e in to_load)
    if a.replace:
        bq_run(["query", "--use_legacy_sql=false",
                f"DELETE FROM `{PROJECT}.{DATASET}.{TABLE}` WHERE entry_number IN ({load_list}) "
                f"AND ingested_at < TIMESTAMP('{run_ts}')"])

    rows = bq_query(f"SELECT entry_number, COUNT(*) AS n, FORMAT('%.2f', SUM(total)) AS total "
                    f"FROM `{PROJECT}.{DATASET}.{TABLE}` WHERE entry_number IN ({load_list}) GROUP BY 1")
    got = {r["entry_number"]: r for r in rows}
    bad = [e["entry_number"] for e in to_load
           if got.get(e["entry_number"], {}).get("n") != "1"
           or got[e["entry_number"]]["total"] != f"{e['total']:.2f}"]
    if bad:
        print(f"\nVERIFY FAILED for {', '.join(bad)}: expected exactly one row with the parsed total. "
              f"Found: {rows}", file=sys.stderr)
        return 6
    for e in to_load:
        print(f"loaded {e['entry_number']}  ${e['total']:,.2f}  [{e['validation_status']}]")
    print(f"\nLoaded {len(to_load)} entr{'y' if len(to_load) == 1 else 'ies'} into {PROJECT}.{DATASET}.{TABLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

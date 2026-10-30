#!/usr/bin/env python3
"""Turn Zendesk complaints into actionable signals for Product, Ops and Sales.

Reads everything from BigQuery (no API keys needed in a cloud session):

  * americanflat.zendesk.tickets / customer_messages   what customers said
  * americanflat.shopify.orders / order_line_items     which SKUs were in the order
  * americanflat.warehouse.shipments_945               which warehouse and carrier shipped it

and writes one JSON file that the "Actionable Complaints" dashboard renders.

Signals
-------
Product   SKUs with repeated damage/defect claims (count and % of units sold),
          and product questions the listing doesn't answer (depth, glass, size...).
Ops       wrong or missing items by SKU, damage by warehouse + carrier,
          lost or late shipments.
Sales     bulk / wholesale leads, out-of-stock demand, requests for products or
          sizes we don't sell.

Classification is keyword-based on the customer's own words (agent replies are
excluded), so it is deterministic and every flag links back to its tickets.

Usage:
  python3 tools/cx_complaint_signals.py --out data.json [--days 90] [--recent 30]
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

PROJECT = "americanflat"
ZD_URL = "https://americanflat.zendesk.com/agent/tickets/"

# A signal is "actionable" when it crosses these in the recent window.
SKU_MIN_COMPLAINTS = 3        # damage/defect claims on one SKU
SKU_MIN_RATE = 0.05           # ...or >= 5% of units sold, with at least 4 claims
PICK_MIN = 2                  # wrong/missing on one SKU
ROUTE_RATIO = 1.25            # warehouse + carrier damage rate vs the average across routes
QUESTION_MIN = 4              # same question on one product family over the window (1+ recent)
OOS_MIN = 2                   # out-of-stock asks for one product family
BULK_MIN_QTY = 25

CARRIERS = {"FEDX": "FedEx", "UPSN": "UPS", "UPS": "UPS", "USPS": "USPS", "USP_": "USPS",
            "AMZX": "Amazon Shipping", "GLOB": "UPS (GLOB)", "CPUP": "UPS (CPUP)", "CTII": "Central Transport",
            "EXLA": "Estes"}

FAMILIES = [
    "Hudson", "Streamline", "Signature", "Epic", "Galleria", "Soho", "Brooklyn",
    "Fusion", "Silhouette", "Veranda", "Legacy", "Hampton", "Metropolitan",
    "Heirloom", "Mod", "Mini Masterpiece", "A-Size", "Canvas Floating",
    "Shadow Box", "Collage", "Easel", "Record Album", "Jersey", "Flag Case",
    "Digital", "Document", "Gallery Wall",
]
FAMILY_ALIASES = {
    "mini masterpiece": "Mini Masterpiece", "kids art": "Mini Masterpiece",
    "canvas float": "Canvas Floating", "floater": "Canvas Floating",
    "shadowbox": "Shadow Box", "shadow box": "Shadow Box",
    "record": "Record Album", "vinyl": "Record Album", "album": "Record Album",
    "jersey": "Jersey", "flag": "Flag Case", "diploma": "Document",
    "certificate": "Document", "document": "Document", "easel": "Easel",
    "collage": "Collage", "a4": "A-Size", "a3": "A-Size", "a2": "A-Size",
    "digital frame": "Digital", "poster frame": "Epic", "gallery wall": "Gallery Wall",
}

def rx(*words):
    return re.compile("|".join(words), re.I)

DAMAGE = rx(r"damag", r"broke", r"crack", r"shatter", r"\bchip", r"\bdent", r"smash",
            r"scratch", r"bent\b", r"arrived in pieces")
TRANSIT = rx(r"glass (was |is )?(broken|cracked|shattered)", r"shatter", r"box (was |is )?(damaged|crushed|torn|open)",
             r"crush", r"smash", r"\bdent", r"arrived (broken|damaged)", r"broken glass", r"cracked glass")
DEFECT = rx(r"came apart", r"coming apart", r"separat", r"\bgap", r"loose", r"warp", r"crooked",
            r"\bglue", r"corner", r"miter", r"mitre", r"hanger (is )?(missing|broke|fell)",
            r"hardware (is )?missing", r"no hardware", r"\bstain", r"smudge", r"residue",
            r"bubble", r"doesn'?t fit", r"does not fit", r"too (small|big|large)", r"wobbl",
            r"peel", r"defect", r"poor quality", r"cheap", r"flimsy", r"tabs? (broke|missing)")
MISSING = rx(r"missing", r"didn'?t (get|receive)", r"did not (get|receive)", r"never (got|received|arrived)",
             r"only (got|received) \d", r"only \d+ (of|out of)", r"not (all|everything)", r"incomplete")
WRONG = rx(r"wrong (item|size|color|colour|frame|product|one)", r"not what i ordered", r"instead of",
           r"incorrect (item|size|color)", r"different (size|color|colour)", r"sent me (a|the) ")
LATE = rx(r"where is my order", r"hasn'?t (arrived|shipped)", r"has not (arrived|shipped)", r"not (yet )?(arrived|shipped)",
          r"\bdelay", r"\bstuck\b", r"no (tracking )?update", r"\blost\b", r"shows delivered",
          r"marked (as )?delivered", r"still waiting", r"haven'?t received")
BULK = rx(r"wholesale", r"\bbulk\b", r"resale", r"resell", r"\bquote\b", r"net ?30", r"purchase order",
          r"(?-i:\bPO\b)(?! ?box)", r"tax[- ]exempt", r"volume (pricing|discount)", r"large order", r"\bcase pricing")
QTY = re.compile(r"\b(\d{2,4})\s*(?:x\s*)?(?:frames|units|pcs|pieces|qty|of them|each|sets?)\b", re.I)
OOS = rx(r"out of stock", r"back in stock", r"restock", r"sold out", r"unavailable",
         r"no longer available", r"when will .{0,40}(available|back)", r"not in stock")
PITCH = rx(r"brand protection", r"we help (brands|businesses|companies)", r"our (agency|services|platform)",
           r"grow your (brand|business|sales)", r"\bseo\b", r"marketing services", r"save on shipping",
           r"shipping (rates|solution)", r"has invited you", r"unsubscribe")
REQUEST = rx(r"do you (make|sell|have|carry|offer)", r"is there a", r"are there any", r"can i (order|get|buy)",
             r"custom (size|cut|frame)", r"looking for")
TOPICS = [
    ("Depth / rabbet", rx(r"rabbet", r"\bdepth", r"how deep", r"thick(ness)?", r"canvas (fit|depth)")),
    ("Outer dimensions", rx(r"outer", r"outside (dimension|measure|size)", r"overall (size|dimension)",
                            r"total size", r"width of the (frame|molding|moulding|wood|border)", r"border width")),
    ("Glass / UV", rx(r"\buv\b", r"glass", r"plexi", r"acrylic", r"glare", r"non-?reflect")),
    ("Hanging", rx(r"\bhang", r"\bwire\b", r"sawtooth", r"horizontal", r"vertical", r"landscape", r"portrait orientation", r"command strip")),
    ("Mat sizing", rx(r"mat (opening|size|window|width)", r"matted to", r"mat border", r"without (the )?mat", r"remove the mat")),
    ("Material / archival", rx(r"acid", r"archival", r"\bmdf\b", r"solid wood", r"real wood", r"material", r"plastic", r"metal")),
    ("Color / finish", rx(r"color", r"colour", r"finish", r"match", r"shade", r"tone")),
    ("Fit / size", rx(r"will (it|my|a) .{0,30}fit", r"fit (a|an|my)", r"what size", r"which size", r"size (do|should) i")),
]
QUESTION = rx(r"\?", r"\bdo you\b", r"\bdoes (it|the|this)\b", r"\bcan (i|it|you)\b", r"\bis (it|the|there)\b",
              r"\bwhat\b", r"\bhow\b", r"\bwhich\b", r"\bwondering\b", r"\bquestion\b")
SIZE = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:\"|in(?:ch(?:es)?)?)?\s*[x×X]\s*(\d{1,2}(?:\.\d)?)\b")


def bq(sql):
    out = subprocess.run(
        ["bq", f"--project_id={PROJECT}", "query", "--nouse_legacy_sql", "--format=json",
         "--max_rows=1000000", sql],
        capture_output=True, text=True)
    if out.returncode:
        sys.exit(f"BigQuery error:\n{out.stderr or out.stdout}")
    return json.loads(out.stdout or "[]")


def fetch(days):
    tickets = bq(f"""
      WITH msgs AS (
        SELECT ticket_id, STRING_AGG(LEFT(COALESCE(plain_body, body), 1500), '\\n' ORDER BY created_at) AS said
        FROM `{PROJECT}.zendesk.customer_messages`
        WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY ticket_id)
      SELECT t.ticket_id, CAST(t.created_at AS STRING) AS created_at, t.status, t.via_channel,
             t.subject, LEFT(t.description, 3000) AS description, t.category,
             TO_JSON_STRING(t.type_of_issue) AS type_of_issue, t.order_reference,
             LEFT(m.said, 6000) AS said
      FROM `{PROJECT}.zendesk.tickets` t
      LEFT JOIN msgs m USING (ticket_id)
      WHERE t.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        AND NOT COALESCE(t.is_deleted, FALSE) AND NOT COALESCE(t.is_junk, FALSE)
        AND COALESCE(t.category, '') NOT IN ('🗑_spam_/_junk', '📲_missed_call')
        AND NOT EXISTS (SELECT 1 FROM UNNEST(t.tags) tag WHERE tag IN ('junk', 'test_chat', 'social_comment'))
    """)
    units = bq(f"""
      SELECT sku, ANY_VALUE(title) AS title, SUM(quantity) AS units, COUNT(DISTINCT order_id) AS orders
      FROM `{PROJECT}.shopify.order_line_items`
      WHERE order_created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY) AND sku IS NOT NULL AND sku != ''
      GROUP BY sku
    """)
    routes = bq(f"""
      SELECT warehouse, carrierScac AS carrier, COUNT(DISTINCT orderNumber) AS orders
      FROM `{PROJECT}.warehouse.shipments_945`
      WHERE shipDate >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
        AND REGEXP_CONTAINS(orderNumber, r'^\\d{{5}}$')
      GROUP BY 1, 2
    """)
    return tickets, units, routes


def order_details(numbers):
    if not numbers:
        return {}
    names = ",".join(f"'#{n}'" for n in sorted(numbers))
    nums = ",".join(f"'{n}'" for n in sorted(numbers))
    rows = bq(f"""
      WITH ship AS (
        SELECT orderNumber, ANY_VALUE(warehouse) AS warehouse, ANY_VALUE(carrierScac) AS carrier
        FROM `{PROJECT}.warehouse.shipments_945` WHERE orderNumber IN ({nums}) GROUP BY 1)
      SELECT REPLACE(o.name, '#', '') AS num, li.sku, li.title, li.quantity, s.warehouse, s.carrier
      FROM `{PROJECT}.shopify.orders` o
      JOIN `{PROJECT}.shopify.order_line_items` li USING (order_id)
      LEFT JOIN ship s ON s.orderNumber = REPLACE(o.name, '#', '')
      WHERE o.name IN ({names})
    """)
    out = defaultdict(lambda: {"lines": [], "warehouse": None, "carrier": None})
    for r in rows:
        o = out[r["num"]]
        o["lines"].append({"sku": r["sku"], "title": r["title"], "qty": int(r["quantity"] or 0)})
        o["warehouse"] = o["warehouse"] or r["warehouse"]
        o["carrier"] = o["carrier"] or r["carrier"]
    return out


def order_number(t):
    for field in (t.get("order_reference"), t.get("subject"), t.get("description"), t.get("said")):
        m = re.search(r"(?<![\d.$])(2\d{4}|3\d{4})(?![\d.])", field or "")
        if m:
            return m.group(1)
    return None


def text_of(t):
    said = t.get("said") or ""
    if not said.strip() or said.startswith("Conversation with"):
        said = t.get("description") or ""
    text = f"{t.get('subject') or ''}\n{said}"
    text = re.sub(r"&nbsp;|&lt;|&gt;|&amp;", " ", text)
    text = re.sub(r"\S+@\S+|https?://\S+", " ", text)
    text = re.sub(r"\+?1?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4}\b", "[phone]", text)
    return text


def family_of(text):
    low = text.lower()
    for f in FAMILIES:
        if f.lower() in low:
            return f
    for alias, f in FAMILY_ALIASES.items():
        if alias in low:
            return f
    return None


def sizes_in(text):
    return {f"{a}x{b}" for a, b in SIZE.findall(text)}


def sku_size_match(sku, sizes):
    digits = re.sub(r"\D", "", sku or "")
    for s in sizes:
        a, b = s.split("x")
        if f"{int(float(a)):02d}{int(float(b)):02d}" in digits:
            return True
    return False


def excerpt(text, pattern=None, width=220):
    flat = re.sub(r"\s+", " ", text).strip()
    if pattern:
        m = pattern.search(flat)
        if m:
            start = max(0, m.start() - 80)
            return ("…" if start else "") + flat[start:start + width] + ("…" if start + width < len(flat) else "")
    return flat[:width] + ("…" if len(flat) > width else "")


def issue_of(t, text):
    """Agents often file complaints under product_detail_inquiry, so the
    customer's words decide; a complaint category or an order number is
    required so newsletters and spam that say "damage" don't count."""
    cat = t.get("category") or ""
    toi = json.loads(t.get("type_of_issue") or "null") or []
    if cat == "orders__wrong_item":
        return "wrong"
    if cat == "order___missing_item":
        return "missing"
    if cat == "order___damage_item" or "damaged_product" in toi:
        return "damaged"
    if not order_number(t):
        return None
    if WRONG.search(text):
        return "wrong"
    damaged = DAMAGE.search(text) or DEFECT.search(text)
    if MISSING.search(text) and not damaged:
        return "missing"
    if damaged:
        return "damaged"
    if "lost_product" in toi or LATE.search(text):
        return "late"
    return None


def build(days, recent):
    tickets, units_rows, route_rows = fetch(days)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=recent)
    units = {r["sku"]: {"units": int(r["units"]), "orders": int(r["orders"]), "title": r["title"]} for r in units_rows}
    route_orders = {(r["warehouse"], r["carrier"]): int(r["orders"]) for r in route_rows}

    for t in tickets:
        t["text"] = text_of(t)
        t["dt"] = datetime.fromisoformat(t["created_at"].replace(" UTC", "+00:00").replace("Z", "+00:00"))
        t["recent"] = t["dt"] >= cutoff
        t["issue"] = issue_of(t, t["text"])
        t["order"] = order_number(t)
    orders = order_details({t["order"] for t in tickets if t["order"] and t["issue"]})

    def ref(t, pattern=None):
        return {"id": int(t["ticket_id"]), "date": t["created_at"][:10], "status": t["status"],
                "channel": t["via_channel"], "order": t["order"], "text": excerpt(t["text"], pattern)}

    # ---- Product + Ops: SKU-level complaints -------------------------------------
    sku = defaultdict(lambda: {"damaged": [], "wrong": [], "missing": [], "shared": 0})
    route = defaultdict(lambda: {"damaged": [], "recent": 0})
    attributed = unattributed = 0
    for t in tickets:
        if t["issue"] not in ("damaged", "wrong", "missing"):
            continue
        o = orders.get(t["order"]) if t["order"] else None
        if not o or not o["lines"]:
            unattributed += 1
            continue
        lines = o["lines"]
        skus = {l["sku"] for l in lines if l["sku"]}
        if len(skus) > 1:
            sizes = sizes_in(t["text"])
            fam = family_of(t["text"])
            narrowed = {l["sku"] for l in lines if l["sku"] and (sku_size_match(l["sku"], sizes)
                        or (fam and fam.lower() in (l["title"] or "").lower()))}
            if narrowed:
                skus = narrowed
        attributed += 1
        entry = ref(t, DAMAGE if t["issue"] == "damaged" else (WRONG if t["issue"] == "wrong" else MISSING))
        for s in skus:
            bucket = sku[s]
            entry = {**entry, "sole": len(skus) == 1}
            if len(skus) > 1:
                bucket["shared"] += 1
            if t["issue"] == "damaged":
                kind = "defect" if DEFECT.search(t["text"]) and not TRANSIT.search(t["text"]) else "transit"
                bucket["damaged"].append({**entry, "kind": kind})
            else:
                bucket[t["issue"]].append(entry)
        if t["issue"] == "damaged" and o["warehouse"]:
            r = route[(o["warehouse"], o["carrier"])]
            r["damaged"].append(entry)
            r["recent"] += t["recent"]

    def recent_count(items):
        return sum(1 for i in items if i["date"] >= cutoff.date().isoformat())

    sku_rows = []
    for s, b in sku.items():
        u = units.get(s, {})
        dmg = b["damaged"]
        sole = [d for d in dmg if d["sole"]]
        n_recent = recent_count(dmg)
        rate = len(sole) / u["units"] if u.get("units") else None
        defect_n = sum(1 for d in dmg if d["kind"] == "defect")
        flag = None
        # Flag on claims where this SKU was the only item in the order, so a
        # multi-item order doesn't count against every SKU in it.
        if recent_count(sole) >= SKU_MIN_COMPLAINTS or (rate and rate >= SKU_MIN_RATE and len(sole) >= 4):
            flag = "Likely product defect" if defect_n * 2 >= len(dmg) else "Packaging / transit damage"
        pick = b["wrong"] + b["missing"]
        sku_rows.append({
            "sku": s, "title": u.get("title") or next((l["title"] for o in orders.values() for l in o["lines"] if l["sku"] == s), s),
            "units": u.get("units"), "damage": len(dmg), "damage_sole": len(sole), "damage_recent": n_recent, "damage_rate": rate,
            "defect": defect_n, "transit": len(dmg) - defect_n, "wrong": len(b["wrong"]), "missing": len(b["missing"]),
            "pick_recent": recent_count(pick), "shared": b["shared"], "flag": flag,
            "pick_flag": "Pick / pack check" if sum(1 for p in pick if p["sole"]) >= PICK_MIN and recent_count(pick) >= 1 else None,
            "tickets": sorted(dmg + pick, key=lambda x: x["date"], reverse=True),
        })
    sku_rows.sort(key=lambda r: (r["flag"] is None, -r["damage_recent"], -r["damage"]))

    route_rows_out = []
    known = [(len(r["damaged"]), route_orders.get(k)) for k, r in route.items() if route_orders.get(k)]
    overall = sum(d for d, _ in known) / sum(o for _, o in known) if known else 0
    for (wh, carrier), r in route.items():
        n_orders = route_orders.get((wh, carrier))
        rate = len(r["damaged"]) / n_orders if n_orders else None
        route_rows_out.append({"warehouse": wh, "carrier": carrier, "damage": len(r["damaged"]), "damage_recent": r["recent"],
                               "orders": n_orders, "rate": rate,
                               "flag": "Damage well above average" if rate and rate >= overall * ROUTE_RATIO and len(r["damaged"]) >= 5 else None,
                               "tickets": sorted(r["damaged"], key=lambda x: x["date"], reverse=True)[:25]})
    route_rows_out.sort(key=lambda r: -(r["rate"] or 0))

    late = [ref(t, LATE) for t in tickets if t["issue"] == "late"]

    # ---- Product: unanswered listing questions ----------------------------------
    questions = defaultdict(list)
    for t in tickets:
        if t["issue"] or not QUESTION.search(t["text"]) or BULK.search(t["text"]):
            continue
        fam = family_of(t["text"]) or "Unspecified product"
        for topic, pat in TOPICS:
            if pat.search(t["text"]):
                questions[(fam, topic)].append(ref(t, pat))
    question_rows = []
    for (fam, topic), items in questions.items():
        n_recent = recent_count(items)
        question_rows.append({"family": fam, "topic": topic, "count": len(items), "recent": n_recent,
                              "flag": "Add to listing" if len(items) >= QUESTION_MIN and n_recent >= 1 and fam != "Unspecified product" else None,
                              "tickets": sorted(items, key=lambda x: x["date"], reverse=True)[:15]})
    question_rows.sort(key=lambda r: (r["flag"] is None, r["family"] == "Unspecified product", -r["recent"], -r["count"]))

    # ---- Sales: leads, out-of-stock demand, requests -----------------------------
    leads = []
    for t in tickets:
        qtys = [int(q) for q in QTY.findall(t["text"]) if 1 < int(q) < 5000]
        if (t.get("category") == "wholesale_inquiry" or BULK.search(t["text"]) or
                (qtys and max(qtys) >= BULK_MIN_QTY)) and t["issue"] not in ("damaged", "wrong", "missing") \
                and not PITCH.search(t["text"]):
            r = ref(t, BULK if BULK.search(t["text"]) else QTY)
            r["qty"] = max(qtys) if qtys else None
            r["family"] = family_of(t["text"])
            r["recent"] = t["recent"]
            leads.append(r)
    leads.sort(key=lambda x: x["date"], reverse=True)

    oos = defaultdict(list)
    for t in tickets:
        if OOS.search(t["text"]) or "oos" in (t.get("type_of_issue") or ""):
            oos[family_of(t["text"]) or "Unspecified product"].append(ref(t, OOS))
    oos_rows = [{"family": f, "count": len(v), "recent": recent_count(v),
                 "flag": "Demand while out of stock" if recent_count(v) >= OOS_MIN and f != "Unspecified product" else None,
                 "tickets": sorted(v, key=lambda x: x["date"], reverse=True)[:15]} for f, v in oos.items()]
    oos_rows.sort(key=lambda r: (r["flag"] is None, -r["recent"], -r["count"]))

    requests = []
    for t in tickets:
        if t["issue"] or not REQUEST.search(t["text"]):
            continue
        sz = sorted(sizes_in(t["text"]))
        if re.search(r"custom", t["text"], re.I) or sz:
            r = ref(t, REQUEST)
            r["sizes"] = sz
            r["custom"] = bool(re.search(r"custom", t["text"], re.I))
            requests.append(r)
    requests.sort(key=lambda x: x["date"], reverse=True)
    size_demand = Counter(s for r in requests for s in r["sizes"])

    flagged = {
        "product": sum(1 for r in sku_rows if r["flag"]) + sum(1 for r in question_rows if r["flag"]),
        "ops": sum(1 for r in sku_rows if r["pick_flag"]) + sum(1 for r in route_rows_out if r["flag"]),
        "sales": sum(1 for r in leads if r["recent"]) + sum(1 for r in oos_rows if r["flag"]),
    }
    return {
        "generated_at": now.isoformat(timespec="minutes"),
        "window_days": days, "recent_days": recent, "zendesk_url": ZD_URL,
        "thresholds": {"sku_min_complaints": SKU_MIN_COMPLAINTS, "sku_min_rate": SKU_MIN_RATE, "pick_min": PICK_MIN,
                       "route_ratio": ROUTE_RATIO, "question_min": QUESTION_MIN, "oos_min": OOS_MIN,
                       "bulk_min_qty": BULK_MIN_QTY},
        "totals": {"tickets": len(tickets),
                   "complaints": sum(1 for t in tickets if t["issue"] in ("damaged", "wrong", "missing")),
                   "attributed": attributed, "unattributed": unattributed, "late": len(late),
                   "flagged": flagged},
        "product": {"skus": [r for r in sku_rows if r["damage"]][:150], "questions": question_rows[:120]},
        "ops": {"route_average": overall, "picks": [r for r in sku_rows if r["wrong"] or r["missing"]][:100], "routes": route_rows_out,
                "late": late[:150]},
        "sales": {"leads": leads[:150], "oos": oos_rows[:60], "requests": requests[:150],
                  "size_demand": size_demand.most_common(25)},
    }


def to_datasets(data):
    """Flatten the report into one table per dashboard dataset."""
    t, recent = data["totals"], data["recent_days"]
    pct = lambda x: round(x, 4) if x is not None else None
    evidence = []

    def cite(signal, tickets, limit=25):
        for k in tickets[:limit]:
            evidence.append({"signal": signal, "ticket_id": k["id"], "date": k["date"], "status": k["status"],
                             "channel": k["channel"], "order": k.get("order"), "kind": k.get("kind") or "",
                             "text": k["text"]})

    sku_damage = []
    for r in data["product"]["skus"]:
        sig = f"sku:{r['sku']}"
        sku_damage.append({"signal": sig, "sku": r["sku"], "product": r["title"], "units": r["units"],
                           "claims": r["damage"], "sole_claims": r["damage_sole"], "recent": r["damage_recent"],
                           "rate": pct(r["damage_rate"]), "defect": r["defect"], "transit": r["transit"],
                           "action": r["flag"] or ""})
        cite(sig, [k for k in r["tickets"] if k.get("kind")])
    questions = []
    for r in data["product"]["questions"]:
        sig = f"q:{r['family']}|{r['topic']}"
        questions.append({"signal": sig, "family": r["family"], "topic": r["topic"], "asks": r["count"],
                          "recent": r["recent"], "action": r["flag"] or ""})
        cite(sig, r["tickets"])
    picks = []
    for r in data["ops"]["picks"]:
        sig = f"pick:{r['sku']}"
        picks.append({"signal": sig, "sku": r["sku"], "product": r["title"], "units": r["units"], "wrong": r["wrong"],
                      "missing": r["missing"], "recent": r["pick_recent"], "action": r["pick_flag"] or ""})
        cite(sig, [k for k in r["tickets"] if not k.get("kind")])
    routes = []
    for r in data["ops"]["routes"]:
        sig = f"route:{r['warehouse']}|{r['carrier']}"
        carrier = CARRIERS.get(r["carrier"], r["carrier"])
        routes.append({"signal": sig, "route": f"{r['warehouse']} · {carrier}", "warehouse": r["warehouse"],
                       "carrier": carrier, "claims": r["damage"], "recent": r["damage_recent"],
                       "orders": r["orders"], "rate": pct(r["rate"]), "average": pct(data["ops"]["route_average"]),
                       "action": r["flag"] or ""})
        cite(sig, r["tickets"])
    leads = [{"ticket_id": r["id"], "date": r["date"], "status": r["status"], "channel": r["channel"],
              "qty": r["qty"], "product": r["family"] or "", "recent": r["recent"], "text": r["text"]}
             for r in data["sales"]["leads"]]
    oos = []
    for r in data["sales"]["oos"]:
        sig = f"oos:{r['family']}"
        oos.append({"signal": sig, "family": r["family"], "asks": r["count"], "recent": r["recent"],
                    "action": r["flag"] or ""})
        cite(sig, r["tickets"])
    sizes = [{"size": s, "asks": n} for s, n in data["sales"]["size_demand"]]
    th = data["thresholds"]
    as_of = datetime.fromisoformat(data["generated_at"]).strftime("%b %-d, %Y %H:%M UTC")
    summary = [{"generated_at": data["generated_at"], "as_of": as_of, "window_days": data["window_days"], "recent_days": recent,
                "sku_min_complaints": th["sku_min_complaints"], "sku_min_rate": th["sku_min_rate"],
                "pick_min": th["pick_min"], "route_ratio": th["route_ratio"], "question_min": th["question_min"],
                "oos_min": th["oos_min"], "bulk_min_qty": th["bulk_min_qty"],
                "route_average": pct(data["ops"]["route_average"]),
                "tickets": t["tickets"], "complaints": t["complaints"], "attributed": t["attributed"],
                "flagged_product": t["flagged"]["product"], "flagged_ops": t["flagged"]["ops"],
                "flagged_sales": t["flagged"]["sales"]}]
    return {"summary": summary, "sku_damage": sku_damage, "questions": questions, "picks": picks,
            "routes": routes, "leads": leads, "oos": oos, "sizes": sizes, "evidence": evidence}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="full report as one JSON file")
    ap.add_argument("--datasets", help="also write one <name>.json per dashboard dataset into this folder")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--recent", type=int, default=30)
    a = ap.parse_args()
    data = build(a.days, a.recent)
    with open(a.out, "w") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    if a.datasets:
        import os
        os.makedirs(a.datasets, exist_ok=True)
        for name, rows in to_datasets(data).items():
            with open(os.path.join(a.datasets, f"{name}.json"), "w") as f:
                json.dump(rows, f, separators=(",", ":"), default=str)
    t = data["totals"]
    print(f"{t['tickets']} tickets, {t['complaints']} complaints ({t['attributed']} matched to an order), "
          f"flagged: {t['flagged']}")


if __name__ == "__main__":
    main()

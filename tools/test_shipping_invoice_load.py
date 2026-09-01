#!/usr/bin/env python3
"""Tests for shipping_invoice_load.py. Run: python3 tools/test_shipping_invoice_load.py

No network and no BigQuery — this covers the parsing half, which is where the
carrier-format risk lives, plus a check that the loader's schema still matches
the DDL in sql/finance_shipping_invoices.sql.
"""
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import shipping_invoice_load as m  # noqa: E402

FEDEX = """Invoice Number,Invoice Date,Bill to Account Number,Express or Ground Tracking ID,Shipment Date,Service Type,Zone Code,Shipper City,Recipient City,Recipient State,Actual Weight Amount,Rated Weight Amount,Original Customer Reference,Original Ref#2,Original Ref#3/PO Number,Tracked Charge Description 1,Tracked Charge Amount 1,Tracked Charge Description 2,Tracked Charge Amount 2,Net Charge Amount
8-123-45678,08/24/2026,123456789,283945019283,08/19/2026,FedEx Home Delivery,5,FONTANA,AUSTIN,TX,3.2,4.0,TARG-8891,,PO-556677,Transportation Charge,"$8.45",Fuel Surcharge,$1.32,"$9.77"
8-123-45678,08/24/2026,123456789,,08/22/2026,,,,,,,,,,,Adjustment - Billing Correction,($12.00),,,($12.00)
"""

STAMPS = """Tracking #,Ship Date,Service,Amount Paid,Adjusted Amount,Weight,Cost Code,Order ID,Reference 1,Printed Message,To City,To State
9400 1000 0000 1234 5678 90,08/19/2026,USPS Ground Advantage,$5.42,,1 lbs 4 oz,TARGET,113-4455,TARG-8891,Target Order 113-4455,Austin,TX
9400 1000 0000 1234 5678 91,08/20/2026,USPS Priority Mail,9.18,$10.44,2 lbs 0 oz,SHOPIFY,SH-99120,SHOP-1201,Shopify,Seattle,WA
"""

UPS = """Invoice Date,Invoice Number,Account Number,Pickup Date,Tracking Number,Service Level,Charge Description,Net Amount,Entered Weight,Billed Weight,Zone,Shipment Reference Number 1,Shipment Reference Number 2
2026-08-24,0000W1234X567,W1234X,2026-08-19,1Z1234X50312345678,UPS Ground,Ground Commercial,11.23,5.0,6.0,004,PO-556677,TARG-8891
2026-08-24,0000W1234X567,W1234X,2026-08-19,1Z1234X50312345678,UPS Ground,Fuel Surcharge,1.85,5.0,6.0,004,PO-556677,TARG-8891
"""

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text)
    return p


def main():
    with tempfile.TemporaryDirectory() as tmp:
        fedex = write(tmp, "fedex_invoice.csv", FEDEX)
        stamps = write(tmp, "stamps_print_history.csv", STAMPS)
        ups = write(tmp, "ups_billing.csv", UPS)

        # --- carrier detection comes from the headers, not the filename ------
        carrier, rows, skipped = m.parse_file(fedex)
        check("fedex carrier", carrier, "FedEx")
        # Two source rows -> three charge lines: the tracked charges are the
        # truth, and the net charge is only their total.
        check("fedex rows", len(rows), 3)
        check("fedex total", round(sum(r["amount"] for r in rows), 2), -2.23)
        first = rows[0]
        check("fedex tracking", first["tracking_number"], "283945019283")
        check("fedex ship_date", first["ship_date"], "2026-08-19")
        check("fedex invoice_date", first["invoice_date"], "2026-08-24")
        check("fedex po", first["reference_3"], "PO-556677")
        check("fedex weight", first["weight_lbs"], 3.2)
        check("fedex billed weight", first["billed_weight_lbs"], 4.0)
        check("fedex base category", first["charge_category"], "base")
        check("fedex fuel category", rows[1]["charge_category"], "fuel")
        # The unmapped net charge survives in raw, so the line total stays
        # reconcilable against what the carrier printed.
        check("fedex raw keeps net charge",
              '"net_charge_amount": "$9.77"' in (first["raw"] or ""), True)
        # Parenthesised money is a credit, not a positive number.
        check("fedex credit", rows[2]["amount"], -12.0)
        check("fedex credit category", rows[2]["charge_category"], "adjustment")

        carrier, rows, _ = m.parse_file(stamps)
        check("stamps carrier", carrier, "Stamps.com")
        check("stamps rows", len(rows), 2)
        check("stamps tracking spaces stripped",
              rows[0]["tracking_number"], "9400100000001234567890")
        check("stamps lbs+oz weight", rows[0]["weight_lbs"], 1.25)
        check("stamps default description", rows[0]["charge_description"], "Postage")
        check("stamps printed message -> reference_2",
              rows[0]["reference_2"], "Target Order 113-4455")
        check("stamps order id", rows[0]["order_id"], "113-4455")
        check("stamps adjusted amount", rows[1]["adjusted_amount"], 10.44)
        check("stamps no adjustment stays null", rows[0]["adjusted_amount"], None)
        check("stamps has no invoice number", rows[0]["invoice_number"], None)

        carrier, rows, _ = m.parse_file(ups)
        check("ups carrier", carrier, "UPS")
        # UPS already bills one row per charge, so rows pass through 1:1.
        check("ups rows", len(rows), 2)
        check("ups total", round(sum(r["amount"] for r in rows), 2), 13.08)
        check("ups ship_date from pickup date", rows[0]["ship_date"], "2026-08-19")
        check("ups zone keeps leading zeros", rows[0]["zone"], "004")
        check("ups reference", rows[0]["reference_1"], "PO-556677")

        # --- row_id is stable across re-parses, so a reload is detectable ---
        _, again, _ = m.parse_file(ups)
        check("row_id deterministic",
              [r["row_id"] for r in again], [r["row_id"] for r in rows])
        check("row_id unique within a file",
              len({r["row_id"] for r in rows}), len(rows))

        # Identical repeated charge lines must not collide.
        dup = write(tmp, "ups_dup.csv", UPS + UPS.splitlines()[1] + "\n")
        _, dup_rows, _ = m.parse_file(dup)
        check("duplicate lines get distinct row_ids",
              len({r["row_id"] for r in dup_rows}), len(dup_rows))

        # --- a file we cannot identify fails loudly rather than half-loading -
        junk = write(tmp, "junk.csv", "alpha,beta\n1,2\n")
        try:
            m.parse_file(junk)
            failures.append("unknown carrier: expected ParseError")
        except m.ParseError as exc:
            check("unknown carrier names the fix", "--carrier" in str(exc), True)

        # ... unless the operator names the carrier.
        headerless = write(tmp, "forced.csv",
                           "Tracking Number,Charge Description,Net Amount\n"
                           "1Z999,Ground,5.00\n")
        carrier, rows, _ = m.parse_file(headerless, carrier_override="UPS")
        check("forced carrier", (carrier, len(rows)), ("UPS", 1))

        # --- money and date parsing ---------------------------------------
        check("money commas", m.parse_money("$1,234.50"), 1234.50)
        check("money parens", m.parse_money("(12.00)"), -12.0)
        check("money blank", m.parse_money("  "), None)
        check("money dash", m.parse_money("-"), None)
        check("date iso", m.parse_date("2026-08-19"), "2026-08-19")
        check("date us", m.parse_date("08/19/2026"), "2026-08-19")
        check("date with time", m.parse_date("08/19/2026 14:32"), "2026-08-19")
        check("date junk", m.parse_date("not a date"), None)
        check("weight oz column", m.parse_weight("20", "weight_oz"), 1.25)

    # --- the loader's schema must match the deployed DDL -------------------
    ddl = (Path(__file__).parents[1] / "sql" / "finance_shipping_invoices.sql").read_text()
    body = ddl.split("CREATE TABLE IF NOT EXISTS", 1)[1].split("PARTITION BY", 1)[0]
    declared = re.findall(r"^\s{2}(\w+)\s+(STRING|DATE|NUMERIC|TIMESTAMP)\b",
                          body, re.M)
    check("schema matches DDL", declared, m.SCHEMA)

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()

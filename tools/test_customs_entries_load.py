#!/usr/bin/env python3
"""Tests for tools/customs_entries_load.py. Run: python3 tools/test_customs_entries_load.py

The fixture is `pdftotext -layout` of a real Worldwide Logistics 7501
(entry 8NW-1591298-8, Sep 2026). Its arithmetic is internally consistent, but
the line values sum to $2,575 against a declared $11,745 - the case the
validator exists to catch.
"""
import sys, unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import customs_entries_load as cel  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "testdata" / "cbp7501_8NW-1591298-8.txt"


class Sample7501(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = FIXTURE.read_text(encoding="utf-8")
        cls.e = cel.parse_7501(cls.text)
        cls.findings = cel.validate(cls.e)

    def test_header(self):
        e = self.e
        self.assertEqual(e["entry_number"], "8NW-1591298-8")
        self.assertEqual(e["filer_code"], "8NW")
        self.assertEqual(e["entry_type"], "01")
        self.assertEqual(str(e["entry_date"]), "2026-09-20")
        self.assertEqual(str(e["summary_date"]), "2026-09-29")
        self.assertEqual(e["port_code"], "2704")
        self.assertEqual(e["bill_of_lading"], "HLCUNG12607TYYG2")
        self.assertEqual(e["importing_carrier"], "DELAWARE EXPRESS (HLCU)")
        self.assertEqual(e["country_of_origin"], "CN")
        self.assertEqual(e["importer_name"], "AMERICANFLAT CORPORATION")
        self.assertEqual(e["broker_name"], "WORLDWIDE LOGISTICS CHB LLC")
        self.assertEqual(e["broker_file_number"], "BCHB260900400")
        self.assertEqual(e["po_numbers"], ["7P4F4YTM", "73IU68GE", "8VJL69WN", "6K2LGG8L", "5UP5FHGW"])
        self.assertEqual(e["location_of_goods"], "W185/MAERSK SEALAND - APM TERMINALS")

    def test_totals(self):
        e = self.e
        self.assertEqual(e["total_entered_value"], Decimal("11745.00"))
        self.assertEqual(e["duty"], Decimal("607.15"))
        self.assertEqual(e["tax"], Decimal("0.00"))
        self.assertEqual(e["other_fees"], Decimal("36.80"))
        self.assertEqual(e["total"], Decimal("643.95"))
        self.assertEqual({f["code"]: f["amount"] for f in e["fees"]},
                         {"499": Decimal("33.58"), "501": Decimal("3.22")})

    def test_lines(self):
        lines = self.e["lines"]
        self.assertEqual([ln["line_number"] for ln in lines], [1, 2, 3])
        self.assertEqual([ln["entered_value"] for ln in lines],
                         [Decimal(1534), Decimal(200), Decimal(841)])
        self.assertEqual([ln["charges"] for ln in lines],
                         [Decimal(1787), Decimal(233), Decimal(980)])
        self.assertEqual([ln["description"] for ln in lines],
                         ["OTHER;WOODEN FRAMES, OTHER", "HSEHLD ART.N/COATED,N/FOOD CNT", "PICTURE FRAMES"])
        self.assertEqual([len(ln["tariffs"]) for ln in lines], [3, 4, 2])
        self.assertEqual(lines[0]["tariffs"][0]["rate"], "12.5%")  # stray "n" from "Cents" stripped
        self.assertEqual(lines[0]["invoice_number"], "001/KMLT10036")
        self.assertEqual(sum(f["amount"] for ln in lines for f in ln["line_fees"] if f["code"] == "501"),
                         Decimal("3.22"))

    def test_commercial_invoice(self):
        (inv,) = self.e["commercial_invoices"]
        self.assertEqual(inv["invoice_number"], "001/KMLT10036")
        self.assertEqual(inv["invoice_value"], Decimal("17497.05"))
        self.assertEqual(inv["freight_deduction"], Decimal("3000.00"))
        self.assertEqual(inv["duty_deduction"], Decimal("2752.46"))
        self.assertEqual(inv["entered_value"], Decimal("11744.59"))

    def test_form_arithmetic_passes(self):
        # Chapter 99 duty is charged on the commodity row's value, MPF is
        # raised to the FY2026 minimum - neither may raise an error.
        self.assertEqual([f for f in self.findings if f["severity"] == "error"], [])

    def test_flags_the_undervalued_lines(self):
        flags = {f["check_name"]: f for f in self.findings if f["severity"] == "flag"}
        self.assertEqual(set(flags), {"entered_value_total", "duty_deduction"})
        self.assertEqual(flags["entered_value_total"]["actual"], Decimal(2575))
        self.assertEqual(flags["entered_value_total"]["expected"], Decimal("11745.00"))
        self.assertIn("2,769.31", flags["entered_value_total"]["message"])

    def test_tampered_duty_is_an_error(self):
        bad = cel.parse_7501(self.text.replace("59.83", "69.83", 1))
        checks = {f["check_name"] for f in cel.validate(bad) if f["severity"] == "error"}
        self.assertIn("tariff_duty", checks)
        self.assertIn("duty_total", checks)

    def test_mpf_below_minimum_is_an_error(self):
        bad = cel.parse_7501(self.text.replace("$33.58", "$8.91", 1))
        self.assertIn("fee_summary", {f["check_name"] for f in cel.validate(bad) if f["severity"] == "error"})

    def test_unknown_fiscal_year_is_info_not_error(self):
        later = cel.parse_7501(self.text.replace("09/20/2026", "10/20/2026", 1))
        self.assertEqual(str(later["entry_date"]), "2026-10-20")
        sev = {f["check_name"]: f["severity"] for f in cel.validate(later)}
        self.assertEqual(sev.get("mpf_limits"), "info")

    def test_not_a_7501(self):
        with self.assertRaises(cel.ParseError):
            cel.parse_7501("INVOICE\nLine Haul  1  905.00\n")


if __name__ == "__main__":
    unittest.main()

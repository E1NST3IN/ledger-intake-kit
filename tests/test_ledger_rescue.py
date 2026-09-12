#!/usr/bin/env python3
"""Tests for ledger-rescue. Run: python3 -m pytest -q (or python3 tests/test_ledger_rescue.py)."""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ledger_rescue as L  # noqa: E402


# ---------------------------------------------------------------- amounts ----
def test_amount_plain():
    assert L.parse_amount("1234.56") == (123456, None)

def test_amount_currency_and_thousands():
    assert L.parse_amount("$1,234.56") == (123456, None)
    assert L.parse_amount("R$ 2.500,00", decimal_sep=",", thousands_sep=".") == (250000, None)

def test_amount_european_thousands_only():
    # 1.234 -> 1234 EUR-style when decimal sep is comma
    assert L.parse_amount("1.234", decimal_sep=",", thousands_sep=".") == (123400, None)

def test_amount_parentheses_negative():
    assert L.parse_amount("(45.10)") == (-4510, None)

def test_amount_trailing_minus_negative():
    assert L.parse_amount("45.10-") == (-4510, None)

def test_amount_ambiguous_no_sep_rule():
    # "1,234" with default decimal="." thousands="," is thousands -> 123400
    assert L.parse_amount("1,234") == (123400, None)

def test_amount_ambiguous_escalated():
    assert L.parse_amount("1,23") == (None, L.E_AMB_NUM)

def test_amount_unparseable():
    assert L.parse_amount("n/a") == (None, L.E_BAD_NUM)

def test_amount_empty():
    assert L.parse_amount("") == (None, L.E_EMPTY)

def test_fmt_cents_roundtrip():
    assert L.fmt_cents(-5) == "-0.05"
    assert L.fmt_cents(100) == "1.00"


# ------------------------------------------------------------------ dates ----
def test_date_iso_passthrough():
    assert L.parse_date("2026-01-31") == ("2026-01-31", None)

def test_date_named_month():
    assert L.parse_date("Jan 2, 2026") == ("2026-01-02", None)
    assert L.parse_date("02-Jan-2026") == ("2026-01-02", None)

def test_date_unambiguous_numeric():
    assert L.parse_date("13/01/2026") == ("2026-01-13", None)  # 13 must be day

def test_date_ambiguous_escalated():
    assert L.parse_date("01/02/2026") == (None, L.E_AMB_DATE)

def test_date_order_opt_in():
    assert L.parse_date("01/02/2026", order="dmy") == ("2026-02-01", None)
    assert L.parse_date("01/02/2026", order="mdy") == ("2026-01-02", None)

def test_date_impossible_escalated():
    assert L.parse_date("31/31/2026", order="dmy") == (None, L.E_BAD_DATE)


# ---------------------------------------------------------------- headers ----
def test_role_inference():
    assert L.infer_role("Transaction Date") == "date"
    assert L.infer_role("Running Balance") == "balance"
    assert L.infer_role("Money Out") == "debit"
    assert L.infer_role("Money In") == "credit"
    assert L.infer_role("Description") == "description"
    assert L.infer_role("FITID") == "id"
    assert L.infer_role("Bogus") == "other"

def test_role_short_token_not_substring_matched():
    # "Balance" must not match the "in" token, "Within" must not match "in"
    assert L.infer_role("Within") != "credit"

def test_dedupe_headers():
    assert L.dedupe_headers(["Amount", "amount", ""]) == ["amount", "amount_2", "col"]


# ---------------------------------------------------------------- engine -----
def _run(rows, **kw):
    return L.repair(rows, **kw)

def test_never_invents_value_on_unparseable():
    rows = [["Date", "Description", "Amount"],
            ["2026-01-02", "Coffee", "n/a"]]
    header, out, audit = _run(rows)
    assert out[0]["amount"] == "n/a"  # written through byte-identical
    assert any(e["reason"] == L.E_BAD_NUM for e in audit["escalations"])

def test_change_is_recorded_and_reversible():
    rows = [["Date", "Amount"], ["2026-01-02", "$1,000.00"]]
    _, out, audit = _run(rows)
    assert out[0]["amount"] == "1000.00"
    ch = [c for c in audit["changed_cells"] if c["column"] == "amount"][0]
    assert ch["from"] == "$1,000.00"
    assert ch["to"] == "1000.00"

def test_debit_credit_split_resolved():
    rows = [["Date", "Withdrawal", "Deposit", "Balance"],
            ["2026-01-02", "50.00", "", "950.00"]]
    _, out, audit = _run(rows)
    assert out[0]["amount"] == "-50.00"

def test_both_debit_and_credit_escalated():
    rows = [["Date", "Debit", "Credit"], ["2026-01-02", "5.00", "9.00"]]
    _, out, audit = _run(rows)
    assert any(e["reason"] == L.E_BOTH_DC for e in audit["escalations"])

def test_balance_chain_ok():
    rows = [["Date", "Amount", "Balance"],
            ["2026-01-02", "100.00", "1100.00"],
            ["2026-01-03", "-50.00", "1050.00"]]
    _, _, audit = _run(rows)
    assert audit["balance_chain"]["ok"] is True
    assert audit["balance_chain"]["checked"] == 1

def test_balance_chain_break_detected():
    rows = [["Date", "Amount", "Balance"],
            ["2026-01-02", "100.00", "1100.00"],
            ["2026-01-03", "-50.00", "1200.00"]]  # wrong balance
    _, _, audit = _run(rows)
    assert audit["balance_chain"]["ok"] is False
    brk = audit["balance_chain"]["breaks"][0]
    assert brk["row"] == 3
    assert brk["discrepancy"] == "150.00"

def test_opening_balance_check():
    rows = [["Date", "Amount", "Balance"], ["2026-01-02", "100.00", "1100.00"]]
    _, _, audit = _run(rows, opening_balance=100000)
    assert audit["balance_chain"]["opening_ok"] is True

def test_duplicate_rows_dropped_and_logged():
    rows = [["Date", "Amount"], ["2026-01-02", "5.00"], ["2026-01-02", "5.00"]]
    _, out, audit = _run(rows)
    assert len(out) == 1
    assert audit["dropped_rows"][0]["row"] == 3

def test_conflicting_duplicate_id_escalated():
    rows = [["Date", "Amount", "Reference"],
            ["2026-01-02", "5.00", "TXN1"],
            ["2026-01-03", "9.00", "TXN1"]]
    _, _, audit = _run(rows)
    assert any(e["reason"] == L.E_CONFLICT_DUP for e in audit["escalations"])

def test_schema_mismatch_escalated():
    rows = [["Date", "Amount"], ["2026-01-02", "5.00", "extra"]]
    _, out, audit = _run(rows)
    assert any(e["reason"] == L.E_SCHEMA for e in audit["escalations"])

def test_nbsp_and_bom_cleaned():
    rows = [["Date", "Amount"], ["\ufeff2026-01-02\u00a0", "\u00a0$5.00\u00a0"]]
    _, out, audit = _run(rows)
    assert out[0]["date"] == "2026-01-02"
    assert out[0]["amount"] == "5.00"

def test_no_amount_column_raises():
    rows = [["Date", "Description"], ["2026-01-02", "x"]]
    try:
        _run(rows)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ---------------------------------------------------------------- e2e CLI ----
def test_cli_end_to_end(tmp_path):
    src = tmp_path / "messy.csv"
    src.write_text(
        "Transaction Date,Money In,Money Out,Running Balance,Reference\n"
        "01/02/2026,,45.10,954.90,TXN-A\n"
        "03/02/2026,1000.00,,1954.90,TXN-B\n"
        "03/02/2026,1000.00,,1954.90,TXN-B\n",
        encoding="utf-8")
    outdir = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "ledger_rescue.py"),
         str(src), "--out", str(outdir), "--date-order", "dmy"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert os.path.exists(outdir / "clean.csv")
    assert os.path.exists(outdir / "escalations.csv")
    audit = json.loads((outdir / "audit.json").read_text())
    assert audit["rows_out"] == 2
    assert audit["balance_chain"]["ok"] is True


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    import tempfile
    from pathlib import Path
    for fn in fns:
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)

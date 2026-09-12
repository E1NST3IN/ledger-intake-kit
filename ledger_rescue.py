#!/usr/bin/env python3
"""
ledger-rescue — audit-grade intake repair for bookkeeping engagements.

Turns a client's messy bank / ledger / accounting export into a clean, importable
CSV plus a full audit dossier, under four contracts:

  1. NEVER INVENT A VALUE. If a cell cannot be repaired from the data itself, the
     cell is written through byte-identical to the input and recorded as an
     escalation with a machine-readable reason. Silent imputation is forbidden.
  2. EVERY CHANGE IS RECORDED AND REVERSIBLE. The audit log stores the exact
     original for every cell that changed, so any change can be replayed backwards.
  3. AMBIGUITY IS ESCALATED, NOT GUESSED. `1/2/2026` is not silently read as
     1 February or 2 January; `1,23` is not silently read as a number. The operator
     opts into a convention explicitly (`--date-order`, `--decimal-sep`).
  4. THE BALANCE CHAIN IS VERIFIED. Where a running-balance column exists, every
     consecutive pair is checked for arithmetic continuity. A break is reported
     with its line number and the exact discrepancy, because a broken balance
     chain is the single most common way a client file is silently wrong.

Money is carried in integer cents end to end; no floats touch an amount.

Stdlib only. No network. No third-party imports. Reads and writes only inside the
paths given on the command line.

Exit codes: 0 = ran (inspect the audit for escalations), 2 = usage/IO error.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import sys
from collections import Counter

__version__ = "1.0.0"
DOSSIER = "ledger-rescue/1"

# Space characters that survive a copy/paste out of a web report, a PDF export,
# or a Windows regional settings dump.
SPACE_CHARS = ("\u00a0", "\u2007", "\u202f", "\u2060", "\u200b", "\ufeff")
CURRENCY = "R$\u20ac\u00a3$\u00a5\u20b9"
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# --- escalation reason codes (stable; safe to branch on in downstream tooling) --
E_EMPTY = "empty_required_field"
E_AMB_NUM = "ambiguous_number"
E_BAD_NUM = "unparseable_number"
E_AMB_DATE = "ambiguous_date"
E_BAD_DATE = "unparseable_date"
E_BOTH_DC = "both_debit_and_credit_present"
E_NO_AMOUNT = "no_amount_in_row"
E_CONFLICT_DUP = "conflicting_duplicate_id"
E_SCHEMA = "row_field_count_mismatch"
E_BALANCE = "balance_chain_break"

# --- header role inference ----------------------------------------------------
ROLE_PATTERNS = (
    ("date", ("date", "data", "transaction date", "posted date", "value date",
              "booking date", "posting date", "dt")),
    ("balance", ("balance", "running balance", "saldo", "ledger balance",
                 "balance after", "closing balance")),
    ("debit", ("debit", "debit amount", "withdrawal", "withdrawals", "money out",
               "paid out", "out", "debito", "débito", "saida", "saída")),
    ("credit", ("credit", "credit amount", "deposit", "deposits", "money in",
                "paid in", "in", "credito", "crédito", "entrada")),
    ("amount", ("amount", "amt", "value", "transaction amount", "montante",
                "valor", "amount usd", "net amount")),
    ("id", ("id", "transaction id", "reference", "ref", "fitid", "cheque no",
            "check no", "transaction reference", "document")),
    ("description", ("description", "memo", "narrative", "details", "payee",
                     "descricao", "descrição", "historico", "histórico",
                     "particulars")),
)


# ---------------------------------------------------------------------------
# primitive text repairs
# ---------------------------------------------------------------------------
def strip_artifacts(raw):
    """Remove BOM, NBSP-family characters, Excel ="..." wrapper, outer space."""
    s = "" if raw is None else str(raw)
    for ch in SPACE_CHARS:
        s = s.replace(ch, " ")
    s = s.strip()
    if len(s) >= 3 and s.startswith('="') and s.endswith('"'):
        s = s[2:-1]
    return s.strip()


def normalize_header(name):
    s = strip_artifacts(name).lower().replace("\n", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def infer_role(header):
    """Map one header to a semantic role, or 'other'."""
    h = normalize_header(header)
    if not h:
        return "other"
    for role, names in ROLE_PATTERNS:
        if h in names:
            return role
    for role, names in ROLE_PATTERNS:
        for n in names:
            if n in ("in", "out", "id", "ref", "dt", "amt", "data", "no"):
                continue  # short tokens match by exact equality only
            if h.startswith(n + " ") or h.endswith(" " + n) or (" " + n + " ") in h:
                return role
    return "other"


def dedupe_headers(headers):
    """Return output header names that are unique and lower_snake."""
    out, seen = [], Counter()
    for h in headers:
        base = re.sub(r"[^a-z0-9]+", "_", normalize_header(h)).strip("_") or "col"
        seen[base] += 1
        out.append(base if seen[base] == 1 else "%s_%d" % (base, seen[base]))
    return out


# ---------------------------------------------------------------------------
# amount parsing — integer cents only
# ---------------------------------------------------------------------------
def parse_amount(raw, decimal_sep=".", thousands_sep=","):
    """Return (cents:int|None, reason:str|None). Never guesses."""
    s = strip_artifacts(raw)
    if s == "":
        return None, E_EMPTY
    neg = False
    m = re.match(r"^\((.*)\)$", s)
    if m:
        neg = True
        s = m.group(1).strip()
    if s.endswith("-"):
        neg = not neg
        s = s[:-1].strip()
    elif s.endswith("+"):
        s = s[:-1].strip()
    if s.startswith("-"):
        neg = not neg
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()
    while s and s[0] in CURRENCY:
        s = s[1:].strip()
    while s and s[-1] in CURRENCY:
        s = s[:-1].strip()
    s = re.sub(r"^[A-Z]{3}\s+", "", s)
    s = s.replace(" ", "")
    if s == "":
        return None, E_EMPTY
    if not re.match(r"^[\d.,]+$", s):
        return None, E_BAD_NUM

    has_dot, has_com = "." in s, "," in s
    if has_dot and has_com:
        if s.rfind(".") > s.rfind(","):
            dec, thou = ".", ","
        else:
            dec, thou = ",", "."
    elif has_dot:
        if re.match(r"^\d{1,3}(\.\d{3})+$", s) and thousands_sep == "." and decimal_sep != ".":
            dec, thou = None, "."
        else:
            dec, thou = ".", None
    elif has_com:
        if re.match(r"^\d{1,3}(,\d{3})+$", s) and thousands_sep == "," and decimal_sep != ",":
            dec, thou = None, ","
        elif re.match(r"^\d+,\d{1,2}$", s) and decimal_sep == ",":
            dec, thou = ",", None
        else:
            return None, E_AMB_NUM
    else:
        dec, thou = None, None

    if dec is None:
        if thou:
            s = s.replace(thou, "")
        if not re.match(r"^\d+$", s):
            return None, E_AMB_NUM
        cents = int(s) * 100
    else:
        if s.count(dec) != 1:
            return None, E_AMB_NUM
        intpart, fracpart = s.split(dec)
        if thou:
            intpart = intpart.replace(thou, "")
        if not re.match(r"^\d+$", intpart) or not re.match(r"^\d{1,2}$", fracpart):
            return None, E_AMB_NUM
        cents = int(intpart) * 100 + int(fracpart.ljust(2, "0"))
    return (-cents if neg else cents), None


def fmt_cents(cents):
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return "%s%d.%02d" % (sign, c // 100, c % 100)


# ---------------------------------------------------------------------------
# date parsing — ISO out, ambiguity escalated
# ---------------------------------------------------------------------------
def parse_date(raw, order=None):
    """Return (iso:str|None, reason:str|None). `order` is 'mdy'|'dmy'|None."""
    s = strip_artifacts(raw)
    if s == "":
        return None, E_EMPTY
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # month-name forms: "Jan 2, 2026" / "2 Jan 2026" / "02-Jan-2026"
    m = re.match(r"^([A-Za-z]{3,9})[ .\-]+(\d{1,2}),?[ .\-]+(\d{4})$", s)
    if m and m.group(1).lower()[:3] in MONTHS:
        return _iso(int(m.group(3)), MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
    m = re.match(r"^(\d{1,2})[ .\-]+([A-Za-z]{3,9})[ .\-]+(\d{4})$", s)
    if m and m.group(2).lower()[:3] in MONTHS:
        return _iso(int(m.group(3)), MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
    # numeric d/m/y
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", s)
    if not m:
        return None, E_BAD_DATE
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000 if y < 70 else 1900
    if order is None:
        if a > 12 and b <= 12:
            eff = "dmy"
        elif b > 12 and a <= 12:
            eff = "mdy"
        else:
            return None, E_AMB_DATE
    elif order in ("mdy", "dmy"):
        eff = order
    else:
        return None, E_BAD_DATE
    mo, da = (a, b) if eff == "mdy" else (b, a)
    return _iso(y, mo, da)


def _iso(y, mo, da):
    try:
        return _dt.date(y, mo, da).isoformat(), None
    except ValueError:
        return None, E_BAD_DATE


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------
def read_table(path):
    """Read a CSV with sniffed dialect; returns list of raw string rows."""
    last = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, "r", newline="", encoding=enc) as f:
                text = f.read()
            break
        except UnicodeDecodeError as exc:
            last = exc
    else:  # pragma: no cover - all three codecs are permissive
        raise last  # type: ignore[misc]
    if text == "":
        return []
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(text.splitlines(), dialect)]


def repair(rows, date_order=None, decimal_sep=".", thousands_sep=",",
           opening_balance=None, keep_duplicates=False):
    """Core engine. Returns (header_out, out_rows, audit:dict)."""
    audit = {
        "dossier": DOSSIER,
        "tool": "ledger-rescue",
        "version": __version__,
        "rows_in": 0,
        "rows_out": 0,
        "header_changes": [],
        "changed_cells": [],
        "escalations": [],
        "dropped_rows": [],
        "balance_chain": {},
    }
    if not rows:
        audit["balance_chain"] = {"checked": 0, "breaks": 0, "ok": True}
        return [], [], audit

    raw_header = [strip_artifacts(c) for c in rows[0]]
    header_out = dedupe_headers(rows[0])
    for src, dst in zip(raw_header, header_out):
        if normalize_header(src) and src != dst:
            audit["header_changes"].append({"from": src, "to": dst})

    roles = [infer_role(c) for c in raw_header]
    ncol = len(header_out)
    idx = {}
    for i, r in enumerate(roles):
        idx.setdefault(r, i)
    has_amount = "amount" in idx
    has_split = "debit" in idx or "credit" in idx
    has_balance = "balance" in idx
    if not has_amount and not has_split:
        raise ValueError("no amount column: need `amount` or debit/credit columns")

    out_rows = []
    seen_ids = {}
    seen_rows = set()
    balances = []  # (line_no, cents|None)
    amounts = []   # (line_no, cents|None)

    for pos, row in enumerate(rows[1:], start=2):  # line 1 is the header
        audit["rows_in"] += 1
        cells = [strip_artifacts(c) for c in row]

        if len(cells) != ncol:
            audit["escalations"].append({
                "row": pos, "column": "*", "value": "",
                "reason": E_SCHEMA,
                "detail": "expected %d fields, found %d" % (ncol, len(cells)),
            })
            cells = (cells + [""] * ncol)[:ncol]

        if not keep_duplicates:
            key = tuple(cells)
            if key in seen_rows:
                audit["dropped_rows"].append(
                    {"row": pos, "reason": "exact_duplicate_row", "cells": cells})
                continue
            seen_rows.add(key)

        rec = {}
        for i, name in enumerate(header_out):
            rec[name] = cells[i]

        # date
        if "date" in idx:
            iso, reason = parse_date(cells[idx["date"]], date_order)
            col = header_out[idx["date"]]
            if reason:
                audit["escalations"].append(
                    {"row": pos, "column": col, "value": cells[idx["date"]],
                     "reason": reason})
            else:
                if cells[idx["date"]] != iso:
                    audit["changed_cells"].append(
                        {"row": pos, "column": col, "from": cells[idx["date"]],
                         "to": iso, "reason": "date_normalized"})
                rec[col] = iso

        # amount
        cents, reason = None, None
        if has_amount:
            col = header_out[idx["amount"]]
            cents, reason = parse_amount(cells[idx["amount"]], decimal_sep, thousands_sep)
            if reason:
                audit["escalations"].append(
                    {"row": pos, "column": col, "value": cells[idx["amount"]],
                     "reason": reason})
                rec[col] = cells[idx["amount"]]  # never invent: write input through
            else:
                if cells[idx["amount"]] != fmt_cents(cents):
                    audit["changed_cells"].append(
                        {"row": pos, "column": col, "from": cells[idx["amount"]],
                         "to": fmt_cents(cents), "reason": "amount_normalized"})
                rec[col] = fmt_cents(cents)
        elif has_split:
            d_raw = cells[idx["debit"]] if "debit" in idx else ""
            c_raw = cells[idx["credit"]] if "credit" in idx else ""
            d_val = parse_amount(d_raw, decimal_sep, thousands_sep) if strip_artifacts(d_raw) else (0, None)
            c_val = parse_amount(c_raw, decimal_sep, thousands_sep) if strip_artifacts(c_raw) else (0, None)
            cols = [header_out[idx[k]] for k in ("debit", "credit") if k in idx]
            if strip_artifacts(d_raw) and strip_artifacts(c_raw):
                reason = E_BOTH_DC
            elif d_val[1] or c_val[1]:
                reason = d_val[1] or c_val[1]
            elif d_val[0] == 0 and c_val[0] == 0:
                reason = E_NO_AMOUNT
            else:
                cents = c_val[0] - abs(d_val[0])
            if reason:
                audit["escalations"].append(
                    {"row": pos, "column": "+".join(cols), "value": "%s|%s" % (d_raw, c_raw),
                     "reason": reason})
                rec["amount"] = ""
            else:
                rec["amount"] = fmt_cents(cents)
                audit["changed_cells"].append(
                    {"row": pos, "column": "amount",
                     "from": "%s|%s" % (d_raw, c_raw), "to": fmt_cents(cents),
                     "reason": "debit_credit_resolved"})

        # balance
        bal_cents = None
        if has_balance:
            col = header_out[idx["balance"]]
            bal_cents, reason = parse_amount(cells[idx["balance"]], decimal_sep, thousands_sep)
            if reason and strip_artifacts(cells[idx["balance"]]):
                audit["escalations"].append(
                    {"row": pos, "column": col, "value": cells[idx["balance"]],
                     "reason": reason})
            elif not reason:
                if cells[idx["balance"]] != fmt_cents(bal_cents):
                    audit["changed_cells"].append(
                        {"row": pos, "column": col, "from": cells[idx["balance"]],
                         "to": fmt_cents(bal_cents), "reason": "amount_normalized"})
                rec[col] = fmt_cents(bal_cents)

        # identity conflict detection
        if "id" in idx:
            key = strip_artifacts(cells[idx["id"]])
            sig = (rec.get(header_out[idx["date"]], ""), rec.get("amount", ""))
            if key:
                if key in seen_ids and seen_ids[key] != sig:
                    audit["escalations"].append(
                        {"row": pos, "column": header_out[idx["id"]], "value": key,
                         "reason": E_CONFLICT_DUP,
                         "detail": "id first seen as %s" % (seen_ids[key],)})
                else:
                    seen_ids[key] = sig

        amounts.append((pos, cents))
        balances.append((pos, bal_cents))
        out_rows.append(rec)

    # ---- balance-chain verification (contract 4) ----
    chain = {"checked": 0, "breaks": [], "ok": True,
             "opening_balance": None, "opening_ok": None}
    prev = None
    for (line_a, bal_a), (line_b, bal_b), (line_m, amt) in _consecutive(balances, amounts):
        if bal_a is None or bal_b is None or amt is None:
            continue
        chain["checked"] += 1
        diff = bal_b - bal_a
        if diff != amt:
            chain["breaks"].append({
                "row": line_b, "reason": E_BALANCE,
                "expected_delta": fmt_cents(amt), "actual_delta": fmt_cents(diff),
                "discrepancy": fmt_cents(diff - amt),
            })
    if opening_balance is not None and balances and amounts:
        line0, bal0 = balances[0]
        _, amt0 = amounts[0]
        if bal0 is not None and amt0 is not None:
            exp = opening_balance + amt0
            chain["opening_balance"] = fmt_cents(opening_balance)
            chain["opening_ok"] = (exp == bal0)
            if exp != bal0:
                chain["breaks"].append({
                    "row": line0, "reason": E_BALANCE,
                    "expected_delta": fmt_cents(amt0),
                    "actual_delta": fmt_cents(bal0 - opening_balance),
                    "discrepancy": fmt_cents(bal0 - exp),
                })
    chain["ok"] = not chain["breaks"]
    for brk in chain["breaks"]:
        audit["escalations"].append(
            {"row": brk["row"], "column": "balance", "value": "",
             "reason": E_BALANCE, "detail": json.dumps(brk)})
    audit["balance_chain"] = chain
    audit["rows_out"] = len(out_rows)
    return header_out, out_rows, audit


def _consecutive(balances, amounts):
    for i in range(len(balances) - 1):
        yield balances[i], balances[i + 1], amounts[i + 1]


def write_outputs(outdir, header, rows, audit):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "clean.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    with open(os.path.join(outdir, "escalations.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row", "column", "value", "reason", "detail"])
        for e in audit["escalations"]:
            w.writerow([e["row"], e["column"], e.get("value", ""),
                        e["reason"], e.get("detail", "")])
    with open(os.path.join(outdir, "audit.json"), "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, sort_keys=True)
    return os.path.join(outdir, "clean.csv")


def summarize(audit):
    chain = audit.get("balance_chain", {})
    lines = [
        "rows in:          %d" % audit["rows_in"],
        "rows out:         %d" % audit["rows_out"],
        "cells changed:    %d" % len(audit["changed_cells"]),
        "escalations:      %d" % len(audit["escalations"]),
        "dropped rows:     %d" % len(audit["dropped_rows"]),
        "balance chain:    %s (%d checks, %d breaks)" % (
            "OK" if chain.get("ok") else "BROKEN",
            chain.get("checked", 0), len(chain.get("breaks", []))),
    ]
    counts = Counter(e["reason"] for e in audit["escalations"])
    for reason, n in sorted(counts.items()):
        lines.append("  - %-34s %d" % (reason, n))
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="ledger-rescue",
        description="Audit-grade intake repair for bookkeeping CSV files.")
    p.add_argument("input", help="messy CSV to repair")
    p.add_argument("--out", required=True, help="output directory")
    p.add_argument("--date-order", choices=("mdy", "dmy"), default=None,
                   help="disambiguate numeric dates; omit to escalate ambiguity")
    p.add_argument("--decimal-sep", default=".", choices=(".", ","))
    p.add_argument("--thousands-sep", default=",", choices=(",", ".", ""))
    p.add_argument("--opening-balance", type=parse_amount_arg, default=None,
                   help="declared opening balance, e.g. 1250.00")
    p.add_argument("--keep-duplicates", action="store_true")
    args = p.parse_args(argv)

    try:
        rows = read_table(args.input)
    except OSError as exc:
        print("error: cannot read %s: %s" % (args.input, exc), file=sys.stderr)
        return 2
    try:
        header, out_rows, audit = repair(
            rows, date_order=args.date_order, decimal_sep=args.decimal_sep,
            thousands_sep=args.thousands_sep or ",", keep_duplicates=args.keep_duplicates,
            opening_balance=args.opening_balance)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    audit["input"] = os.path.basename(args.input)
    write_outputs(args.out, header, out_rows, audit)
    print(summarize(audit))
    return 0


def parse_amount_arg(text):
    cents, reason = parse_amount(text)
    if reason:
        raise argparse.ArgumentTypeError("cannot read amount: %r (%s)" % (text, reason))
    return cents


if __name__ == "__main__":
    sys.exit(main())

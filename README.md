# Ledger Intake Rescue

**Audit-grade intake repair for bookkeeping engagements.**
Turn a client's messy bank statement, ledger export, or spreadsheet dump into a
clean, importable CSV — with a dossier proving exactly what changed and why.

Stdlib-only Python 3.9+. No dependencies. No network. No uploads.

## The problem it solves

Every bookkeeper and accountant loses the first hours of every new client to the
same job: the client's file doesn't import. Dates are in three formats, amounts are
text, NBSP characters break the parser, debit/credit are split into two columns,
there are duplicate rows, and — the expensive one — the running balance column
doesn't reconcile.

Generic cleaners make this worse for accounting work because they *guess*. A guessed
date or a silently imputed amount is a restatement waiting to happen.

## The four contracts

1. **Never invents a value.** Unrepairable cells are written through byte-identical
   and escalated with a machine-readable reason.
2. **Every change is recorded and reversible.** `audit.json` stores the exact
   original for every changed cell.
3. **Ambiguity is escalated, not guessed.** `01/02/2026` and `1,23` are escalated
   unless the operator explicitly opts in with `--date-order` / `--decimal-sep`.
4. **The balance chain is verified.** Every consecutive pair is checked for
   arithmetic continuity; breaks are reported with line number and discrepancy.

## Usage

```bash
python3 ledger_rescue.py client_statement.csv --out ./out --date-order dmy --opening-balance 950.00
```

Outputs:

| File | What it is |
|---|---|
| `out/clean.csv` | importable data, ISO dates, signed amounts, integer-cent exact |
| `out/escalations.csv` | every cell a human must look at, with reason codes |
| `out/audit.json` | full dossier: changes, drops, header mapping, balance chain |

Exit code 0 = ran (read the audit). Exit code 2 = usage/IO error.

## Tests

```bash
python3 tests/test_ledger_rescue.py     # 32/32
```

## Scope

Single-file CLI, no server, no dependencies. Designed for firms processing client
files in bulk from a workstation. Deliberately not a PDF OCR engine — if you need
scanned-statement OCR, this is not that product.

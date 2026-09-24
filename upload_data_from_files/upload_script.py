"""
Backfills the `financials` table for the last 5 completed FY-start years from
already-downloaded GDFL CSV exports, using the exact same extraction/storage
logic as fetch_historical_financials.py (extract_metrics_with_priority /
resolve_period_date / store_financials_for_symbol, all imported unchanged)
-- just fed from local Label/Value CSV files instead of live
GetFinancialResults API calls.

Per statement type, for each of the last 5 FY-start years
(current_fy_year() - 1 .. - 5, e.g. FY2021-22..FY2025-26 as of this run):
  - ProfitLoss:               the 4 quarters (QJ, QS, QD, QM).
  - BalanceSheet / CashFlow:  the full year (M12) only.

Folder-name convention matches fetch_historical_financials.py/GDFL exactly:
`year` is the FY-start calendar year (April-March), `period` is one of
QJ/QS/QD/QM (calendar quarters ending Jun/Sep/Dec/Mar respectively) or M12
(full year) -- e.g. year=2024, period=QM reads the "2024QM" folder, whose
filings' DateOfEndOfReportingPeriod/DateOfEndOfFinancialYear is 2025-03-31.
`financials.year`/`financials.period` are stored as the *requested* (2024,
"Q4"), not re-derived from that date -- exactly like fetch_historical_financials.py.

Because folder names already encode the company-agnostic standard FY
quarter, there's no need for the old per-symbol fiscal-quarter-type
translation (symbol_with_fin_year.xlsx) or the excel-driven per-metric `keys`
list (new_accepted_rejected_symbols_file.xlsx / get_all_symbols()) that the
previous version of this script used -- both solved a problem (mapping a
company's *own* reported quarter label to a folder name) that doesn't exist
once folders are requested directly by (year, period), the same way
fetch_historical_financials.py requests them from the API. Each statement
type/year/period combo reads both Consolidated and Standalone CSVs (when
present); store_financials_for_symbol() applies its usual Consolidated-
priority-with-Standalone-fallback per metric, same as the live-API path.
Company-specific taxonomy overrides for filings with a blank/missing XbrlUrl
now live in financial_metrics.special_company_types (ported there from this
script's old local special_company_types dict), since identify_report_type()
is shared with the live-API path too.

Only writes to `financials` -- pnl_financials/balance_sheet_financials/
cashflow_financials are out of scope here (that's
fetch_historical_financial_stock_details.py's job, against the live API).

Usage:
    python upload_data_from_files/upload_script.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import pandas as pd
from sqlalchemy.orm import Session

from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import (
    FULL_YEAR_PERIOD,
    STATEMENT_TYPES,
    TRAILING_YEAR_PERIODS,
    current_fy_year,
    store_financials_for_symbol,
)

PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".upload_script_progress.json")


def load_progress() -> dict:
    if not os.path.exists(PROGRESS_FILE):
        return {}
    try:
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_progress(index: int, total: int, symbol: str) -> None:
    with open(PROGRESS_FILE, "w") as f:
        json.dump(
            {
                "last_index": index,
                "total": total,
                "last_symbol": symbol,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
            indent=2,
        )


def prompt_start_index(default_index: int) -> int:
    prompt = f"Start from which index? [default: {default_index}]: "
    try:
        raw = input(prompt).strip()
    except EOFError:
        return default_index
    if not raw:
        return default_index
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid index '{raw}', using default {default_index}.")
        return default_index

consolidated = "/home/decodeup/PycharmProjects/alphaDecodeFinancialTechCrons/upload_data_from_files/data_13_april_2026/Consolidated"
standalone = "/home/decodeup/PycharmProjects/alphaDecodeFinancialTechCrons/upload_data_from_files/data_13_april_2026/Standalone"

profit_path = f"{consolidated}/ProfitLoss"
balance_sheet_path = f"{consolidated}/BalanceSheet"
cashflow_path = f"{consolidated}/CashFlow"

profit_path_s = f"{standalone}/ProfitLoss"
balance_sheet_path_s = f"{standalone}/BalanceSheet"
cashflow_path_s = f"{standalone}/CashFlow"

# statement type -> (Consolidated root, Standalone root)
STATEMENT_PATHS: dict[str, tuple[str, str]] = {
    "ProfitLoss": (profit_path, profit_path_s),
    "BalanceSheet": (balance_sheet_path, balance_sheet_path_s),
    "CashFlow": (cashflow_path, cashflow_path_s),
}

LAST_N_YEARS = 5
# Last 5 completed FY-start years, e.g. [2025, 2024, 2023, 2022, 2021] when
# current_fy_year() is 2026 -- matches the FY2021-22..FY2025-26 folders
# actually present in the CSV dump.
YEARS = [current_fy_year() - i for i in range(1, LAST_N_YEARS + 1)]

# Per-statement-type (year, period) combos, mirroring
# fetch_historical_financials.STATEMENT_YEAR_PERIODS's shape/intent but
# widened to 5 years and to ProfitLoss's full quarterly cycle (this is a
# one-time historical backfill from CSVs already on disk, not a rate-limited
# API pull, so there's no reason to narrow it further).
YEAR_PERIODS_BY_STATEMENT: dict[str, list[tuple[int, str]]] = {
    "ProfitLoss": [(year, period) for year in YEARS for period in TRAILING_YEAR_PERIODS],
    "BalanceSheet": [(year, FULL_YEAR_PERIOD) for year in YEARS],
    "CashFlow": [(year, FULL_YEAR_PERIOD) for year in YEARS],
}


def _csv_items(path: str) -> list[dict] | None:
    """
    Reads a Label/Value CSV into the same [{"Label": ..., "Value": ...}, ...]
    item shape GetFinancialResults' "Value" list uses, so it can be fed
    straight into financial_metrics.py's extraction functions unchanged.
    Returns None if the file doesn't exist (or is empty), meaning "no filing
    for this nature/period" -- same as a missing (nature, year, period)
    combo on the live-API path.

    Some exports in this dump have stray non-UTF-8 bytes (e.g. a literal
    0xA0/NBSP glued onto a numeric value, seen in
    Standalone/CashFlow/2021M12/AIRAN.csv) that make utf-8 decoding fail
    outright. Falling back to latin-1 (a total byte<->codepoint mapping that
    never raises UnicodeDecodeError) reads those bytes as their literal
    characters instead of erroring; financial_metrics._to_number()'s
    str(value).strip() already strips NBSP (it's whitespace per Python's str
    methods), so the value still parses correctly.
    """
    try:
        try:
            df = pd.read_csv(path)
        except UnicodeDecodeError:
            df = pd.read_csv(path, encoding="latin-1")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return None
    return df[["Label", "Value"]].to_dict("records")


def fetch_historical_financials_from_csv(symbol: str) -> dict:
    """
    Same nested shape as fetch_historical_financials.fetch_historical_financials():
        {stmt_type: {year: {period: {nature: {"Value": [...]} or {"error": "..."}}}}}
    sourced from the Consolidated/Standalone CSV folders instead of GDFL's API.
    """
    results: dict = {}
    for stmt_type in STATEMENT_TYPES:
        results[stmt_type] = {}
        consolidated_root, standalone_root = STATEMENT_PATHS[stmt_type]
        for year, period in YEAR_PERIODS_BY_STATEMENT.get(stmt_type, []):
            results[stmt_type].setdefault(year, {})[period] = {}
            for nature, root in (("Consolidated", consolidated_root), ("Standalone", standalone_root)):
                file_path = f"{root}/{year}{period}/{symbol}.csv"
                items = _csv_items(file_path)
                results[stmt_type][year][period][nature] = (
                    {"Value": items} if items is not None else {"error": f"missing file {file_path}"}
                )
    return results


def main():
    parser = argparse.ArgumentParser(description="Backfill `financials` from local GDFL CSV exports.")
    parser.add_argument(
        "--start-index",
        type=int,
        help="1-based index in the symbol list to start from, skipping the interactive prompt "
        "(use this for cron/non-interactive runs).",
    )
    args = parser.parse_args()

    db: Session = SessionLocal()
    try:
        db_symbols = (
            db.query(Symbol).filter(Symbol.symbol.isnot(None)).order_by(Symbol.symbol).all()
        )
        print(f"{len(db_symbols)} symbol(s) in the symbols table; years={YEARS}.")

        total = len(db_symbols)

        progress = load_progress()
        default_start_index = progress.get("last_index", 0) + 1 if progress else 1
        default_start_index = min(default_start_index, total) if total else 1

        if args.start_index is not None:
            start_index = args.start_index
        elif sys.stdin.isatty():
            if progress.get("last_symbol"):
                print(f"Last run stopped at index {progress['last_index']} ({progress['last_symbol']}).")
            start_index = prompt_start_index(default_start_index)
        else:
            start_index = default_start_index

        start_index = max(1, start_index)
        db_symbols = db_symbols[start_index - 1 :]

        rejected_symbols: dict[str, list[str]] = {}
        for idx, db_symbol in enumerate(db_symbols, start=start_index):
            symbol = db_symbol.symbol
            print(f"[{idx}/{total}] {symbol}")

            data = fetch_historical_financials_from_csv(symbol)
            written = store_financials_for_symbol(db, db_symbol.id, symbol, data, rejected_symbols)
            db.commit()
            save_progress(idx, total, symbol)
            if written:
                print(f"  stored/updated {written} metric row(s)")

        if rejected_symbols:
            print("\n===== Symbols with unresolved metrics =====")
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    print(f"  {symbol}: {reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

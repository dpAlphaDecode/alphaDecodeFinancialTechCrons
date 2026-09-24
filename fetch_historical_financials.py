"""
Fetch historical financial statements (Profit & Loss, Balance Sheet, Cash
Flow) for every symbol in the `symbols` DB table from GDFL's Fundamental
Data REST API (see gdfl/client.py -- GetFinancialResults).

Fetches exactly the (FY-start year, period) combos in
STATEMENT_YEAR_PERIODS, which vary by statement type -- currently ProfitLoss
fetches FY2026-27 QM (Q4), FY2026-27 QJ (Q1), and FY2025-26 M12 (full year),
while BalanceSheet and CashFlow only fetch FY2025-26 M12. GDFL's `year` param
is the FY-start calendar year (e.g. year=2025 means FY2025-2026, April-March).

For each statement/year/period this fetches both Consolidated and Standalone
filings; when mapping to headline metrics, Consolidated takes priority and
Standalone is only used to fill in metrics Consolidated doesn't have.

Usage:
    python fetch_historical_financials.py
    python fetch_historical_financials.py --symbols RELIANCE,TCS
    python fetch_historical_financials.py --json-dir out --limit 5
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import time

from dotenv import load_dotenv

from db.models.symbol import Symbol
from db.session import SessionLocal
from financial_metrics import extract_metrics_with_priority, resolve_period_date, store_financials
from gdfl import GDFLAPIError, GDFLClient

load_dotenv()

STATEMENT_TYPES = ("ProfitLoss", "BalanceSheet", "CashFlow")
NATURES_OF_REPORT = ("Consolidated", "Standalone")
TRAILING_YEAR_PERIODS = ("QJ", "QS", "QD", "QM")  # the four quarters
FULL_YEAR_PERIOD = "M12"
DEFAULT_EXCHANGE = "BSE"

# Restrict historical fetches to exactly these (FY-start year, period)
# combos, per statement type -- currently ProfitLoss covers FY2026-27 QM
# (Q4), FY2026-27 QJ (Q1), and FY2025-26 M12 (full year); BalanceSheet and
# CashFlow only cover FY2025-26 M12, since those don't need quarterly
# tracking the way P&L does. Widen a statement type's list or pass an
# explicit `year_periods_by_statement=` to fetch_historical_financials() to
# cover more.
STATEMENT_YEAR_PERIODS: dict[str, list[tuple[int, str]]] = {
    "ProfitLoss": [(2026, "QM"), (2026, "QJ"), (2025, "M12")],
    "BalanceSheet": [(2025, "M12")],
    "CashFlow": [(2025, "M12")],
}

# GDFL quarter code -> Indian financial quarter label stored in `financials.period`.
QUARTER_LABELS = {"QJ": "Q1", "QS": "Q2", "QD": "Q3", "QM": "Q4"}


def current_fy_year(today: _dt.date | None = None) -> int:
    """
    GDFL's `year` param is the FY-start calendar year (e.g. 2024 = FY2024-25).
    Indian FY runs April-March, so before April the FY-start year is last
    calendar year.
    """
    today = today or _dt.date.today()
    return today.year if today.month >= 4 else today.year - 1


def fetch_historical_financials(
    symbol: str,
    exchange: str = DEFAULT_EXCHANGE,
    year_periods_by_statement: dict[str, list[tuple[int, str]]] | None = None,
    client: GDFLClient | None = None,
) -> dict:
    """
    Returns a nested dict:
        {
          "ProfitLoss":   {2026: {"QM": {"Consolidated": {...}, "Standalone": {...}},
                                   "QJ": {...}},
                            2025: {"M12": {...}}},
          "BalanceSheet": {2025: {"M12": {...}}},
          "CashFlow":     {2025: {"M12": {...}}},
        }
    Each nature's leaf is either the raw GetFinancialResults response (dict
    with a "Value" list of line items), or {"error": "..."} if that
    particular (nature, year, period) combination isn't available/enabled
    for your account.

    `year_periods_by_statement` defaults to STATEMENT_YEAR_PERIODS; pass an
    explicit {statement_type: [(FY-start year, period), ...]} dict to fetch
    a different set. A statement type missing from the dict fetches nothing.
    """
    client = client or GDFLClient()
    year_periods_by_statement = year_periods_by_statement or STATEMENT_YEAR_PERIODS

    results: dict = {}
    for stmt_type in STATEMENT_TYPES:
        results[stmt_type] = {}
        for year, period in year_periods_by_statement.get(stmt_type, []):
            results[stmt_type].setdefault(year, {})[period] = {}
            for nature_of_report in NATURES_OF_REPORT:
                try:
                    results[stmt_type][year][period][nature_of_report] = client.get_financial_results(
                        exchange=exchange,
                        instrument_identifier=symbol,
                        year=year,
                        nature_of_report=nature_of_report,
                        type=stmt_type,
                        period=period,
                    )
                except GDFLAPIError as exc:
                    results[stmt_type][year][period][nature_of_report] = {"error": str(exc)}
    return results


def store_financials_for_symbol(
    db,
    symbol_id: int,
    symbol: str,
    data: dict,
    rejected_symbols: dict[str, list[str]],
) -> int:
    """
    Upserts every headline metric found across all statement types/years/
    periods for one symbol into the `financials` table only, preferring
    each metric's Consolidated value and falling back to Standalone.
    """
    written = 0
    for stmt_type, by_year in data.items():
        for year, by_period in by_year.items():
            for period, by_nature in by_period.items():
                consolidated = by_nature.get("Consolidated", {})
                standalone = by_nature.get("Standalone", {})
                consolidated_items = consolidated.get("Value") if "error" not in consolidated else None
                standalone_items = standalone.get("Value") if "error" not in standalone else None
                if not consolidated_items and not standalone_items:
                    continue

                metrics, sources = extract_metrics_with_priority(
                    consolidated_items, standalone_items, symbol, rejected_symbols
                )
                if not metrics:
                    continue

                date = resolve_period_date(consolidated_items, standalone_items)
                if date is None:
                    rejected_symbols.setdefault(symbol, []).append(
                        f"no DateOfEndOfReportingPeriod/DateOfEndOfFinancialYear for {stmt_type} {year} {period}"
                    )
                    continue

                is_annual = period == FULL_YEAR_PERIOD
                frequency = "annual" if is_annual else "quarterly"
                stored_period = "" if is_annual else QUARTER_LABELS.get(period, period)
                written += store_financials(
                    db,
                    symbol_id,
                    metrics,
                    sources,
                    date,
                    frequency=frequency,
                    period=stored_period,
                    year=year,
                )
    return written


def fetch_all_symbols() -> list[Symbol]:
    """Fetch every non-archived symbol from the `symbols` table."""
    db = SessionLocal()
    try:
        return (
            db.query(Symbol)
            .filter(Symbol.archive.is_(False))
            .order_by(Symbol.symbol)
            .all()
        )
    finally:
        db.close()


def _print_summary(symbol: str, data: dict):
    print(f"\n===== {symbol} =====")
    for stmt_type, by_year in data.items():
        for year, by_period in sorted(by_year.items()):
            for period, by_nature in by_period.items():
                for nature_of_report, payload in by_nature.items():
                    if isinstance(payload, dict) and "error" in payload:
                        print(f"  {stmt_type} FY{year}-{year + 1} {period} {nature_of_report}: ERROR - {payload['error'][:100]}")
                    else:
                        items = payload.get("Value", []) if isinstance(payload, dict) else []
                        print(f"  {stmt_type} FY{year}-{year + 1} {period} {nature_of_report}: {len(items)} line items")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch trailing 1-year financial statements for every symbol in the DB."
    )
    parser.add_argument("--symbols", help="Comma-separated symbol list to use instead of the DB (for testing).")
    parser.add_argument("--exchange", help="Override exchange for every symbol (default: each symbol's own `exchange` column, falling back to BSE).")
    parser.add_argument("--json-dir", help="Directory to write one <symbol>.json file per symbol instead of printing a summary.")
    parser.add_argument("--limit", type=int, help="Only process the first N symbols (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.symbols:
            requested = [s.strip() for s in args.symbols.split(",") if s.strip()]
            db_symbols = {
                s.symbol: s for s in db.query(Symbol).filter(Symbol.symbol.in_(requested)).all()
            }
            targets = []
            for sym in requested:
                row = db_symbols.get(sym)
                exchange = "BSE"
                targets.append((sym, exchange, row.id if row else None))
        else:
            symbols = fetch_all_symbols()
            targets = [
                (s.symbol, "BSE", s.id) for s in symbols
            ]

        if args.limit:
            targets = targets[: args.limit]

        if args.json_dir:
            os.makedirs(args.json_dir, exist_ok=True)

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        total = len(targets)
        for idx, (symbol, exchange, symbol_id) in enumerate(targets, start=1):
            print(f"[{idx}/{total}] Fetching {symbol} ({exchange})...")
            data = fetch_historical_financials(symbol, exchange=exchange, client=client)

            if args.json_dir:
                out_path = os.path.join(args.json_dir, f"{symbol}.json")
                with open(out_path, "w") as f:
                    json.dump(data, f, indent=2)
            else:
                _print_summary(symbol, data)

            if symbol_id is None:
                rejected_symbols.setdefault(symbol, []).append("symbol not found in `symbols` table, skipped storing")
            else:
                written = store_financials_for_symbol(
                    db, symbol_id, symbol, data, rejected_symbols
                )
                db.commit()
                print(f"  stored/updated {written} metric rows")

            if args.sleep and idx < total:
                time.sleep(args.sleep)

        if rejected_symbols:
            print("\n===== Symbols with unresolved metrics =====")
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    print(f"  {symbol}: {reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

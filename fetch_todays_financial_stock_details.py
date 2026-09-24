"""
Fills in pnl_financials/balance_sheet_financials/cashflow_financials for
whichever symbols have a financial-results announcement landing today, per
the `financial_calendar` table (populated by fetch_results_calendar.py).

Mirrors fetch_todays_financials.py, but scoped to the detailed per-sector
metric mapping from fetch_historical_financial_stock_details.py: for each
`financial_calendar` row whose `result_date` matches the run date (today,
IST, unless --date overrides it), this fetches that (statement type, year,
period) combo from GDFL for the symbol it belongs to -- both Consolidated
and Standalone -- then upserts via the unchanged
store_detailed_financials_for_symbol()/extract_detailed_metrics()
Consolidated-first-Standalone-fallback logic from
fetch_historical_financial_stock_details.py. Calendar querying/grouping and
the GDFL fetch itself are reused from fetch_todays_financials.py rather than
duplicated.

Usage:
    python fetch_todays_financial_stock_details.py
    python fetch_todays_financial_stock_details.py --date 2026-09-18
    python fetch_todays_financial_stock_details.py --symbols RELIANCE,TCS
"""
from __future__ import annotations

import argparse
import time

from dotenv import load_dotenv

from db.session import SessionLocal
from fetch_historical_financials import DEFAULT_EXCHANGE
from fetch_historical_financial_stock_details import (
    load_metric_mappings,
    store_detailed_financials_for_symbol,
)
from fetch_todays_financials import fetch_due_calendar_targets, fetch_due_financials, resolve_run_date
from gdfl import GDFLClient

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Fetch/store detailed P&L/Balance Sheet/Cash Flow metrics for symbols whose "
                    "financial_calendar result_date is today."
    )
    parser.add_argument(
        "--date", help="Run as if today were this date (YYYY-MM-DD), instead of the actual current IST date."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    args = parser.parse_args()

    load_metric_mappings()  # fail fast if the xlsx is missing/malformed, before hitting the API

    run_date = resolve_run_date(args.date)
    symbols_filter = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    db = SessionLocal()
    try:
        targets, _ = fetch_due_calendar_targets(db, run_date, symbols_filter)
        print(f"{len(targets)} symbol(s) have a financial_calendar entry due {run_date}.")
        if not targets:
            return

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        total = len(targets)
        for idx, (symbol_id, entry) in enumerate(targets.items(), start=1):
            symbol = entry["symbol"]
            combos = entry["combos"]
            print(f"[{idx}/{total}] Fetching {symbol} ({len(combos)} combo(s) due today)...")

            data = fetch_due_financials(symbol, args.exchange, combos, client)
            written = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            print(f"  stored/updated {written} metric rows")

            if args.sleep and idx < total:
                time.sleep(args.sleep)

        if rejected_symbols:
            print("\n===== Symbols with unresolved data =====")
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    print(f"  {symbol}: {reason}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

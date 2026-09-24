"""
Combined "today's results" financials + stock-details fetch/store job.

fetch_todays_financials.py (writes `financials`, Consolidated-priority) and
fetch_todays_financial_stock_details.py (writes pnl_financials/
balance_sheet_financials/cashflow_financials, Consolidated-first with a
per-metric Standalone fallback) each independently call
fetch_due_financials() for the same `financial_calendar`-due (statement
type, year, period, nature) combos, for the same symbols. Running them as
two separate cron jobs means GetFinancialResults gets hit twice per symbol
for identical data, burning through GDFL's 1800-requests/hour cap for no
reason -- same issue as the historical pair, fixed the same way in
fetch_historical_financials_and_stock_details.py.

This script fetches once per symbol and feeds both store paths from that
same response:
  - `financials` gets each metric's Consolidated value, falling back to
    Standalone only where Consolidated doesn't have it
    (store_financials_for_symbol() / extract_metrics_with_priority()).
  - pnl_financials/balance_sheet_financials/cashflow_financials get both
    Consolidated and Standalone considered per metric, Consolidated
    preferred with Standalone as the per-metric fallback
    (store_detailed_financials_for_symbol() / extract_detailed_metrics()).

Calendar querying/grouping and the GDFL fetch itself are reused unchanged
from fetch_todays_financials.py rather than duplicated.

Usage:
    python fetch_todays_financials_and_stock_details.py
    python fetch_todays_financials_and_stock_details.py --date 2026-09-18
    python fetch_todays_financials_and_stock_details.py --symbols RELIANCE,TCS
"""
from __future__ import annotations

import argparse
import datetime as _dt
import time

from dotenv import load_dotenv

from db.models.financial_calendar import FinancialCalendar
from db.session import SessionLocal
from fetch_historical_financials import DEFAULT_EXCHANGE, store_financials_for_symbol
from fetch_historical_financial_stock_details import (
    load_metric_mappings,
    store_detailed_financials_for_symbol,
)
from fetch_results_calendar import IST
from fetch_todays_financials import fetch_due_calendar_targets, fetch_due_financials, resolve_run_date
from gdfl import GDFLClient

load_dotenv()


def _mark_calendar_fetched(db, symbol_id: int, run_date: _dt.date, data: dict) -> None:
    """
    Stamps every `financial_calendar` row this run touched with fetched_at/
    fetch_status, keyed by (symbol_id, run_date, results_type, results_period)
    -- both Consolidated and Standalone calendar rows for a combo get the same
    stamp. status is "success" as soon as GDFL answered without an API error
    for at least one nature -- a combo GDFL can't resolve (e.g. unmapped
    sector) still counts as fetched so the 8:30 AM retry doesn't loop on it
    forever; "failed" means every nature call errored and is worth retrying.
    """
    now = _dt.datetime.now(IST)
    for stmt_type, by_year in data.items():
        for by_period in by_year.values():
            for period, by_nature in by_period.items():
                status = "success" if any("error" not in resp for resp in by_nature.values()) else "failed"
                rows = (
                    db.query(FinancialCalendar)
                    .filter_by(
                        symbol_id=symbol_id,
                        result_date=run_date,
                        results_type=stmt_type,
                        results_period=period,
                    )
                    .all()
                )
                for row in rows:
                    row.fetched_at = now
                    row.fetch_status = status


def run(
    db,
    run_date: _dt.date,
    exchange: str = DEFAULT_EXCHANGE,
    symbols_filter: set[str] | None = None,
    only_unfetched: bool = False,
    sleep: float = 0.0,
) -> dict:
    """
    Callable form of main()'s logic, for use by the scheduler (scheduler/jobs.py)
    as well as the CLI below. Unlike the other refactored scripts' run(), this
    one takes `db` as a required positional arg since the scheduler always
    needs the same session across this call to see the fetch_status stamps
    it writes -- there's no meaningful standalone-session mode here.

    `only_unfetched=True` is the retry-job path: scopes to `financial_calendar`
    rows for `run_date` not yet marked fetch_status="success".
    """
    load_metric_mappings()  # fail fast if Detailed_page_keys.xlsx is missing/malformed, before hitting the API

    targets, _ = fetch_due_calendar_targets(db, run_date, symbols_filter, only_unfetched=only_unfetched)
    print(f"{len(targets)} symbol(s) have a financial_calendar entry due {run_date}.")

    summary = {
        "run_date": str(run_date),
        "symbols_processed": 0,
        "financials_written": 0,
        "detailed_written": 0,
        "rejected": {},
    }
    if not targets:
        return summary

    client = GDFLClient()
    rejected_symbols: dict[str, list[str]] = {}
    total = len(targets)
    for idx, (symbol_id, entry) in enumerate(targets.items(), start=1):
        symbol = entry["symbol"]
        combos = entry["combos"]
        print(f"[{idx}/{total}] Fetching {symbol} ({len(combos)} combo(s) due today)...")

        data = fetch_due_financials(symbol, exchange, combos, client)

        written_financials = store_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
        written_detailed = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
        _mark_calendar_fetched(db, symbol_id, run_date, data)
        db.commit()
        print(
            f"  stored/updated {written_financials} financials row(s), "
            f"{written_detailed} detailed metric row(s)"
        )

        summary["symbols_processed"] += 1
        summary["financials_written"] += written_financials
        summary["detailed_written"] += written_detailed

        if sleep and idx < total:
            time.sleep(sleep)

    if rejected_symbols:
        summary["rejected"] = rejected_symbols
        print("\n===== Symbols with unresolved data =====")
        for symbol, reasons in rejected_symbols.items():
            for reason in reasons:
                print(f"  {symbol}: {reason}")

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Fetch GetFinancialResults once per symbol for today's financial_calendar-due combos "
        "and store into both `financials` (Consolidated-priority) and "
        "pnl_financials/balance_sheet_financials/cashflow_financials (Consolidated+Standalone per metric)."
    )
    parser.add_argument(
        "--date", help="Run as if today were this date (YYYY-MM-DD), instead of the actual current IST date."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    parser.add_argument(
        "--only-unfetched",
        action="store_true",
        help="Restrict to financial_calendar rows not yet marked fetch_status=success (retry mode).",
    )
    args = parser.parse_args()

    run_date = resolve_run_date(args.date)
    symbols_filter = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    db = SessionLocal()
    try:
        run(
            db,
            run_date,
            exchange=args.exchange,
            symbols_filter=symbols_filter,
            only_unfetched=args.only_unfetched,
            sleep=args.sleep,
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()

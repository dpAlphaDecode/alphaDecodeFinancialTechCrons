"""
Fills in `financials` for whichever symbols have a financial-results
announcement landing today, per the `financial_calendar` table (populated
by fetch_results_calendar.py).

For each `financial_calendar` row whose `result_date` matches the run date
(today, IST, unless --date overrides it), this fetches that (statement
type, year, period) combo from GDFL for the symbol it belongs to -- both
Consolidated and Standalone, exactly like fetch_historical_financials.py
does for its fixed trailing-quarters window -- then upserts into
`financials` via the unchanged
store_financials_for_symbol()/extract_metrics_with_priority()
Consolidated-first-Standalone-fallback logic from fetch_historical_financials.py.

`financial_calendar.results_type` values outside STATEMENT_TYPES
(ProfitLoss/BalanceSheet/CashFlow) -- e.g. FR, RelatedPartyTransactions --
aren't understood by the `financials` mapping and are skipped.

`financial_calendar.results_period` values outside ALLOWED_PERIODS (the
standard quarters QJ/QS/QD/QM plus the full year M12) are skipped too -- e.g.
M6, which some companies file semi-annually instead of quarterly. These
filings often omit DateOfEndOfReportingPeriod entirely, in which case
financial_metrics._period_end_date() falls back to DateOfEndOfFinancialYear
-- the end of that company's *whole* FY, not of the period actually being
reported -- which fy_year_and_quarter() then mislabels under the
hardcoded April-March FY assumption every non-standard-FY company breaks.
Simplest fix: never fetch/store these periods at all.

Usage:
    python fetch_todays_financials.py
    python fetch_todays_financials.py --date 2026-09-18
    python fetch_todays_financials.py --symbols RELIANCE,TCS
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import time

from dotenv import load_dotenv

from sqlalchemy import or_

from db.models.financial_calendar import FinancialCalendar
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import (
    DEFAULT_EXCHANGE,
    FULL_YEAR_PERIOD,
    NATURES_OF_REPORT,
    STATEMENT_TYPES,
    TRAILING_YEAR_PERIODS,
    store_financials_for_symbol,
)
from fetch_results_calendar import IST
from gdfl import GDFLAPIError, GDFLClient
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

# The only results_period values this pipeline fetches/stores -- the four
# standard quarters plus the full year. Anything else (e.g. M6 semi-annual
# filings) is skipped; see module docstring.
ALLOWED_PERIODS = set(TRAILING_YEAR_PERIODS) | {FULL_YEAR_PERIOD}


def resolve_run_date(cli_date: str | None) -> _dt.date:
    if cli_date:
        return _dt.datetime.strptime(cli_date, "%Y-%m-%d").date()
    return _dt.datetime.now(IST).date()


def fetch_due_calendar_targets(
    db, run_date: _dt.date, symbols_filter: set[str] | None = None, only_unfetched: bool = False
) -> tuple[dict[int, dict], list[str]]:
    """
    Returns ({symbol_id: {"symbol": str, "combos": {(stmt_type, year, period), ...}}}, skipped_results_types)
    for every `financial_calendar` row landing on `run_date`, grouped by
    symbol and deduped across nature_of_report -- both natures are always
    fetched together per combo, same as fetch_historical_financials.py.

    `only_unfetched=True` (used by the scheduler's next-morning retry job)
    restricts this to rows whose `fetch_status` isn't already "success" --
    i.e. combos a previous run either never attempted or failed to fetch.
    """
    query = (
        db.query(FinancialCalendar, Symbol.symbol)
        .join(Symbol, Symbol.id == FinancialCalendar.symbol_id)
        .filter(FinancialCalendar.result_date == run_date)
    )
    if symbols_filter:
        query = query.filter(Symbol.symbol.in_(symbols_filter))
    if only_unfetched:
        query = query.filter(
            or_(FinancialCalendar.fetch_status.is_(None), FinancialCalendar.fetch_status != "success")
        )

    targets: dict[int, dict] = {}
    skipped_types: set[str] = set()
    skipped_periods: set[str] = set()
    for cal, symbol in query.all():
        if cal.results_type not in STATEMENT_TYPES:
            skipped_types.add(cal.results_type)
            continue
        if cal.results_period not in ALLOWED_PERIODS:
            skipped_periods.add(cal.results_period)
            continue
        entry = targets.setdefault(cal.symbol_id, {"symbol": symbol, "combos": set()})
        entry["combos"].add((cal.results_type, cal.result_year, cal.results_period))

    if skipped_types:
        logger.warning("skipping calendar rows with unsupported results_type: %s", sorted(skipped_types))
    if skipped_periods:
        logger.warning("skipping calendar rows with unsupported results_period: %s", sorted(skipped_periods))
    return targets, sorted(skipped_types)


def fetch_due_financials(
    symbol: str,
    exchange: str,
    combos: set[tuple[str, int, str]],
    client: GDFLClient,
) -> dict:
    """
    Same nested {stmt_type: {year: {period: {nature: response}}}} shape as
    fetch_historical_financials(), scoped to just the combos due today.
    """
    data: dict = {}
    for stmt_type, year, period in combos:
        data.setdefault(stmt_type, {}).setdefault(year, {})[period] = {}
        for nature_of_report in NATURES_OF_REPORT:
            try:
                data[stmt_type][year][period][nature_of_report] = client.get_financial_results(
                    exchange=exchange,
                    instrument_identifier=symbol,
                    year=year,
                    nature_of_report=nature_of_report,
                    type=stmt_type,
                    period=period,
                )
            except GDFLAPIError as exc:
                data[stmt_type][year][period][nature_of_report] = {"error": str(exc)}
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Fetch/store `financials` for symbols whose financial_calendar result_date is today."
    )
    parser.add_argument(
        "--date", help="Run as if today were this date (YYYY-MM-DD), instead of the actual current IST date."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    args = parser.parse_args()

    configure_logging("fetch_todays_financials")
    run_date = resolve_run_date(args.date)
    symbols_filter = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    db = SessionLocal()
    try:
        targets, _ = fetch_due_calendar_targets(db, run_date, symbols_filter)
        logger.info("%s symbol(s) have a financial_calendar entry due %s.", len(targets), run_date)
        if not targets:
            return

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        total = len(targets)
        for idx, (symbol_id, entry) in enumerate(targets.items(), start=1):
            symbol = entry["symbol"]
            combos = entry["combos"]
            logger.info("[%s/%s] Fetching %s (%s combo(s) due today)...", idx, total, symbol, len(combos))

            data = fetch_due_financials(symbol, args.exchange, combos, client)
            written = store_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            logger.info("stored/updated %s metric rows", written)

            if args.sleep and idx < total:
                time.sleep(args.sleep)

        if rejected_symbols:
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    logger.warning("%s: %s", symbol, reason)
    finally:
        db.close()


if __name__ == "__main__":
    main()

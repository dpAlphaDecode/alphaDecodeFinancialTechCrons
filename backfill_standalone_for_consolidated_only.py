"""
For symbols that already have Consolidated data in pnl_financials/
balance_sheet_financials/cashflow_financials but are missing Standalone
(the "Only Consolidated" bucket in standalone_consolidated_report.xlsx),
fetches ONLY the Standalone nature from GDFL's GetFinancialResults and
stores it into those same three detailed tables.

Why Standalone-only, not both natures: fetching both and running them
through store_detailed_financials_for_symbol() unchanged would have
Consolidated win the per-metric priority merge again (it's already there
for virtually every mapped metric), so Standalone would still never get
written -- that's the normal behavior this same store function gives
every other symbol, and is exactly why this bucket exists in the first
place. Fetching ONLY Standalone means consolidated_items is empty for
every combo, so extract_detailed_metrics() falls back to Standalone for
every mapped metric, and upsert_detailed_metric() writes it under
statement_type="standalone" -- a new row alongside the existing
"consolidated" row (the tables' UniqueConstraint includes statement_type/
nature, so this never overwrites the existing Consolidated row).

Deliberately does NOT touch the `financials` table: store_financials_for_symbol()
upserts financials keyed on (symbol_id, metric_name, date) only -- no
nature in that key -- so feeding it Standalone-only data would silently
overwrite the already-correct Consolidated-derived value there instead of
adding a row alongside it. Only pnl_financials/balance_sheet_financials/
cashflow_financials -- the tables standalone_consolidated_report.xlsx's
Only-Consolidated bucket is actually about -- are updated.

Usage:
    python backfill_standalone_for_consolidated_only.py --symbols AARVI,ABCOTS,...
    python backfill_standalone_for_consolidated_only.py --symbols-file path/to/list.json
"""
from __future__ import annotations

import argparse
import json
import logging

from dotenv import load_dotenv

from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import DEFAULT_EXCHANGE, STATEMENT_TYPES, STATEMENT_YEAR_PERIODS
from fetch_historical_financial_stock_details import load_metric_mappings, store_detailed_financials_for_symbol
from gdfl import GDFLAPIError, GDFLClient
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)


def fetch_standalone_only(
    symbol: str, exchange: str, client: GDFLClient, year_periods_by_statement: dict
) -> dict:
    """Same nested shape fetch_historical_financials() returns, but only ever populates the 'Standalone' key."""
    results: dict = {}
    for stmt_type in STATEMENT_TYPES:
        results[stmt_type] = {}
        for year, period in year_periods_by_statement.get(stmt_type, []):
            results[stmt_type].setdefault(year, {})[period] = {}
            try:
                results[stmt_type][year][period]["Standalone"] = client.get_financial_results(
                    exchange=exchange,
                    instrument_identifier=symbol,
                    year=year,
                    nature_of_report="Standalone",
                    type=stmt_type,
                    period=period,
                )
            except GDFLAPIError as exc:
                results[stmt_type][year][period]["Standalone"] = {"error": str(exc)}
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Standalone-only data for symbols that already have Consolidated in "
        "pnl/balance_sheet/cashflow_financials, and store it into those same 3 tables (financials untouched)."
    )
    parser.add_argument("--symbols", help="Comma-separated symbol list.")
    parser.add_argument("--symbols-file", help="Path to a JSON file containing a list of symbol strings.")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    args = parser.parse_args()

    if not args.symbols and not args.symbols_file:
        raise SystemExit("Pass --symbols or --symbols-file.")

    if args.symbols_file:
        with open(args.symbols_file) as f:
            requested = json.load(f)
    else:
        requested = [s.strip() for s in args.symbols.split(",") if s.strip()]

    configure_logging("backfill_standalone_for_consolidated_only")
    load_metric_mappings()

    db = SessionLocal()
    try:
        db_symbols = {s.symbol: s for s in db.query(Symbol).filter(Symbol.symbol.in_(requested)).all()}
        targets = [(sym, db_symbols[sym].id) for sym in requested if sym in db_symbols]
        for sym in requested:
            if sym not in db_symbols:
                logger.warning("%s: not found in `symbols` table, skipped", sym)

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        total = len(targets)
        for idx, (symbol, symbol_id) in enumerate(targets, start=1):
            logger.info("[%s/%s] Fetching Standalone-only for %s (%s)...", idx, total, symbol, args.exchange)
            data = fetch_standalone_only(symbol, args.exchange, client, STATEMENT_YEAR_PERIODS)
            written = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            logger.info("stored/updated %s detailed metric row(s)", written)

        if rejected_symbols:
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    logger.warning("%s: %s", symbol, reason)
    finally:
        db.close()


if __name__ == "__main__":
    main()

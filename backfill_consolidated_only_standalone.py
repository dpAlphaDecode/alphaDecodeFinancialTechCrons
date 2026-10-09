"""
One-off backfill for symbols that are "consolidated only" -- i.e. they
already have Consolidated rows in pnl_financials/balance_sheet_financials/
cashflow_financials (and, for the headline-metric priority table, in
`financials`) but are missing Standalone.

fetch_historical_financials_and_stock_details.py's missing_year_periods_for_symbol()
gate only checks whether a (year, frequency, period) row exists for a symbol
in each table -- it does not look at which `nature of report` (Consolidated
vs Standalone) that row holds. So for these symbols, every combo already
looks "fully covered" to that gate and gets skipped, meaning a normal rerun
of that script never re-fetches Standalone.

This script reuses the exact same fetch/store functions from
fetch_historical_financials.py / fetch_historical_financial_stock_details.py
unchanged, but calls fetch_historical_financials() with the *full*
STATEMENT_YEAR_PERIODS directly, bypassing the nature-blind gate, so
Standalone (and anything else actually missing) gets fetched and the
Consolidated+Standalone-aware store functions do the usual upsert.

Usage:
    python backfill_consolidated_only_standalone.py --symbols SYM1,SYM2,...
"""
from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import (
    DEFAULT_EXCHANGE,
    STATEMENT_YEAR_PERIODS,
    fetch_historical_financials,
    store_financials_for_symbol,
)
from fetch_historical_financial_stock_details import (
    load_metric_mappings,
    store_detailed_financials_for_symbol,
)
from gdfl import GDFLClient
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Force a full Consolidated+Standalone re-fetch for a specific symbol list "
        "(use for symbols stuck as consolidated-only by the normal script's nature-blind skip gate)."
    )
    parser.add_argument("--symbols", required=True, help="Comma-separated symbol list.")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--sleep", type=float, default=0.0)
    args = parser.parse_args()

    configure_logging("backfill_consolidated_only_standalone")
    load_metric_mappings()

    requested = [s.strip() for s in args.symbols.split(",") if s.strip()]

    db = SessionLocal()
    try:
        db_symbols = {s.symbol: s for s in db.query(Symbol).filter(Symbol.symbol.in_(requested)).all()}
        targets = [(sym, db_symbols[sym].id) for sym in requested if sym in db_symbols]
        missing_in_db = [sym for sym in requested if sym not in db_symbols]
        for sym in missing_in_db:
            logger.warning("%s: not found in `symbols` table, skipped", sym)

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        total = len(targets)
        for idx, (symbol, symbol_id) in enumerate(targets, start=1):
            logger.info("[%s/%s] Forcing full re-fetch for %s (%s)...", idx, total, symbol, args.exchange)
            data = fetch_historical_financials(
                symbol,
                exchange=args.exchange,
                client=client,
                year_periods_by_statement=STATEMENT_YEAR_PERIODS,
            )
            written_financials = store_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            written_detailed = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            logger.info(
                "stored/updated %s financials row(s), %s detailed metric row(s)",
                written_financials, written_detailed,
            )
            if args.sleep and idx < total:
                import time
                time.sleep(args.sleep)

        if rejected_symbols:
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    logger.warning("%s: %s", symbol, reason)
    finally:
        db.close()


if __name__ == "__main__":
    main()

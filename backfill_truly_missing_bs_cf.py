"""
Narrower version of backfill_bs_cf_from_report.py: instead of trusting the
xlsx report's "missing" classification as-is (which flags a (symbol, year)
combo as missing even if it already has *some* rows -- just not judged
"complete"), this re-checks against the DB directly and only fetches
(symbol, year, statement_type) combos that have ZERO existing rows in
balance_sheet_financials/cashflow_financials. Combos that already have at
least one row are skipped entirely, since the earlier broad run showed most
"missing" combos already had partial data and only needed a metric-level
top-up (or nothing) rather than a full refetch.

Same fetch (fetch_historical_financials) and store
(store_detailed_financials_for_symbol) logic as backfill_bs_cf_from_report.py
-- only the target-selection step differs.

Usage:
    python backfill_truly_missing_bs_cf.py --plan-only
    python backfill_truly_missing_bs_cf.py --limit 5
    python backfill_truly_missing_bs_cf.py
"""
from __future__ import annotations

import argparse
import logging
import time

from dotenv import load_dotenv

from backfill_bs_cf_from_report import REPORT_PATH, load_missing_bs_cf_years
from db.models.balance_sheet_financials import BalanceSheetFinancial
from db.models.cashflow_financials import CashflowFinancial
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financial_stock_details import (
    load_metric_mappings,
    store_detailed_financials_for_symbol,
)
from fetch_historical_financials import DEFAULT_EXCHANGE, FULL_YEAR_PERIOD, fetch_historical_financials
from gdfl import GDFLClient
from gdfl.client import DEFAULT_RATE_LIMIT_PER_HOUR
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

TABLE_MODEL = {"BalanceSheet": BalanceSheetFinancial, "CashFlow": CashflowFinancial}


def existing_year_keys(db, model) -> set[tuple[int, int]]:
    """{(symbol_id, year)} for every (symbol_id, year) that already has at least one annual row."""
    rows = (
        db.query(model.symbol_id, model.year)
        .filter(model.frequency == "annual")
        .distinct()
        .all()
    )
    return {(r.symbol_id, r.year) for r in rows}


def filter_to_truly_missing(db, all_targets: dict[int, dict]) -> tuple[dict[int, dict], int]:
    existing = {stmt: existing_year_keys(db, model) for stmt, model in TABLE_MODEL.items()}

    filtered: dict[int, dict] = {}
    dropped_combos = 0
    for symbol_id, entry in all_targets.items():
        new_entry = {"symbol": entry["symbol"]}
        for stmt in ("BalanceSheet", "CashFlow"):
            years = [y for y in entry[stmt] if (symbol_id, y) not in existing[stmt]]
            dropped_combos += len(entry[stmt]) - len(years)
            new_entry[stmt] = years
        if new_entry["BalanceSheet"] or new_entry["CashFlow"]:
            filtered[symbol_id] = new_entry
    return filtered, dropped_combos


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=REPORT_PATH)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--limit", type=int, help="Only process the first N truly-missing symbols.")
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    configure_logging("backfill_truly_missing_bs_cf")
    load_metric_mappings()

    all_targets = load_missing_bs_cf_years(args.report)
    print(f"Symbols flagged by the report: {len(all_targets)}")

    db = SessionLocal()
    try:
        targets, dropped_combos = filter_to_truly_missing(db, all_targets)
        print(f"(symbol, year, statement) combos already having >=1 row in DB (skipped): {dropped_combos}")
        print(f"Symbols with at least one TRULY missing combo: {len(targets)}")

        if args.symbols:
            requested = {s.strip() for s in args.symbols.split(",") if s.strip()}
            targets = {sid: e for sid, e in targets.items() if e["symbol"] in requested}

        items = list(targets.items())
        if args.limit:
            items = items[: args.limit]
        targets = dict(items)

        total_combos = sum(len(e["BalanceSheet"]) + len(e["CashFlow"]) for e in targets.values())
        total_calls = total_combos * 2
        print(f"Symbols selected for this run: {len(targets)}")
        print(f"  Truly-missing year-combos: {total_combos}  ->  {total_calls} GetFinancialResults calls")
        print(f"  GDFL account rate limit: {DEFAULT_RATE_LIMIT_PER_HOUR} requests/hour")
        if total_calls > DEFAULT_RATE_LIMIT_PER_HOUR:
            hours = -(-total_calls // DEFAULT_RATE_LIMIT_PER_HOUR)
            print(f"  Expect >= {hours} hour(s) wall-clock from rate-limit sleeps alone.")

        if args.plan_only:
            return

        db_symbols = {s.id: s for s in db.query(Symbol).filter(Symbol.id.in_(targets.keys())).all()}
        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        summary = {"symbols_processed": 0, "rows_written": 0}

        total = len(targets)
        for idx, (symbol_id, entry) in enumerate(targets.items(), start=1):
            symbol = entry["symbol"]
            db_symbol = db_symbols.get(symbol_id)
            if db_symbol is None or db_symbol.symbol != symbol:
                rejected_symbols.setdefault(symbol, []).append(f"symbol_id {symbol_id} not found/mismatch, skipped")
                continue

            year_periods_by_statement = {
                stmt_type: [(year, FULL_YEAR_PERIOD) for year in entry[stmt_type]]
                for stmt_type in ("BalanceSheet", "CashFlow")
                if entry[stmt_type]
            }

            logger.info(
                "[%s/%s] Fetching %s: BS years=%s, CF years=%s",
                idx, total, symbol, entry["BalanceSheet"], entry["CashFlow"],
            )
            data = fetch_historical_financials(
                symbol, exchange=DEFAULT_EXCHANGE, year_periods_by_statement=year_periods_by_statement, client=client,
            )
            written = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            logger.info("stored/updated %s metric rows for %s", written, symbol)

            summary["symbols_processed"] += 1
            summary["rows_written"] += written
            if args.sleep and idx < total:
                time.sleep(args.sleep)

        print(f"\nDone. Symbols processed: {summary['symbols_processed']}, metric rows written/updated: {summary['rows_written']}")
        if rejected_symbols:
            print(f"Symbols with issues: {len(rejected_symbols)}")
            for symbol, reasons in rejected_symbols.items():
                for reason in reasons:
                    logger.warning("%s: %s", symbol, reason)
    finally:
        db.close()


if __name__ == "__main__":
    main()

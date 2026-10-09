"""
PNL counterpart to backfill_truly_missing_bs_cf.py: parses the "All Symbols"
sheet's PNL:<year>Q<n> missing-period tokens (consolidated_missing_periods/
standalone_missing_periods -- PNL is tracked quarterly only in that report,
there's no PNL:<year> M12 token), unions consolidated+standalone per symbol,
then -- same lesson learned from the BS/CF run -- re-checks against
pnl_financials directly and only fetches (symbol, year, quarter) combos that
have ZERO existing rows, skipping ones the report calls "missing" but that
already have at least partial data.

GDFL quarter-code mapping (same as QUARTER_LABELS elsewhere in this repo):
Q1->QJ, Q2->QS, Q3->QD, Q4->QM. The report's "year" in PNL:<year>Q<n> is the
FY-start year, matching pnl_financials.year directly (same convention
check_financials_availability.py uses).

Same fetch (fetch_historical_financials) and store
(store_detailed_financials_for_symbol) logic as the BS/CF scripts.

Usage:
    python backfill_truly_missing_pnl.py --plan-only
    python backfill_truly_missing_pnl.py --limit 5
    python backfill_truly_missing_pnl.py
"""
from __future__ import annotations

import argparse
import logging
import re
import time

from dotenv import load_dotenv

from backfill_bs_cf_from_report import REPORT_PATH, SHEET_NAME, _parse_periods
from db.models.pnl_financials import PnlFinancial
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financial_stock_details import (
    load_metric_mappings,
    store_detailed_financials_for_symbol,
)
from fetch_historical_financials import DEFAULT_EXCHANGE, fetch_historical_financials
from gdfl import GDFLClient
from gdfl.client import DEFAULT_RATE_LIMIT_PER_HOUR
from logging_setup import configure_logging

import openpyxl

load_dotenv()

logger = logging.getLogger(__name__)

PNL_PERIOD_RE = re.compile(r"^PNL:(\d{4})Q([1-4])$")
QUARTER_LABEL_TO_CODE = {"Q1": "QJ", "Q2": "QS", "Q3": "QD", "Q4": "QM"}


def load_missing_pnl_year_quarters(path: str = REPORT_PATH) -> dict[int, dict]:
    """{symbol_id: {"symbol": str, "quarters": {(year, "QJ"/"QS"/"QD"/"QM"), ...}}}"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[SHEET_NAME]
    rows = ws.iter_rows(min_row=2, values_only=True)

    out: dict[int, dict] = {}
    for row in rows:
        (
            symbol_id, symbol, name, listed_date, cons_complete, stan_complete,
            classification, no_rating_cons, no_rating_stan,
            cons_missing_count, stan_missing_count, cons_missing, stan_missing,
        ) = row

        quarters: set[tuple[int, str]] = set()
        for token in _parse_periods(cons_missing) + _parse_periods(stan_missing):
            m = PNL_PERIOD_RE.match(token)
            if not m:
                continue
            year, q = m.groups()
            quarters.add((int(year), QUARTER_LABEL_TO_CODE[f"Q{q}"]))

        if quarters:
            out[symbol_id] = {"symbol": symbol, "quarters": quarters}
    return out


def existing_pnl_keys(db) -> set[tuple[int, int, str]]:
    """{(symbol_id, year, period_label)} for every quarterly pnl_financials row."""
    rows = (
        db.query(PnlFinancial.symbol_id, PnlFinancial.year, PnlFinancial.period)
        .filter(PnlFinancial.frequency == "quarterly")
        .distinct()
        .all()
    )
    return {(r.symbol_id, r.year, r.period) for r in rows}


CODE_TO_LABEL = {"QJ": "Q1", "QS": "Q2", "QD": "Q3", "QM": "Q4"}


def filter_to_truly_missing(db, all_targets: dict[int, dict]) -> tuple[dict[int, dict], int]:
    existing = existing_pnl_keys(db)

    filtered: dict[int, dict] = {}
    dropped = 0
    for symbol_id, entry in all_targets.items():
        kept = {
            (year, code) for year, code in entry["quarters"]
            if (symbol_id, year, CODE_TO_LABEL[code]) not in existing
        }
        dropped += len(entry["quarters"]) - len(kept)
        if kept:
            filtered[symbol_id] = {"symbol": entry["symbol"], "quarters": kept}
    return filtered, dropped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=REPORT_PATH)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--limit", type=int, help="Only process the first N truly-missing symbols.")
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    configure_logging("backfill_truly_missing_pnl")
    load_metric_mappings()

    all_targets = load_missing_pnl_year_quarters(args.report)
    print(f"Symbols flagged by the report (PNL): {len(all_targets)}")

    db = SessionLocal()
    try:
        targets, dropped = filter_to_truly_missing(db, all_targets)
        print(f"(symbol, year, quarter) combos already having >=1 row in DB (skipped): {dropped}")
        print(f"Symbols with at least one TRULY missing PNL quarter: {len(targets)}")

        if args.symbols:
            requested = {s.strip() for s in args.symbols.split(",") if s.strip()}
            targets = {sid: e for sid, e in targets.items() if e["symbol"] in requested}

        items = list(targets.items())
        if args.limit:
            items = items[: args.limit]
        targets = dict(items)

        total_combos = sum(len(e["quarters"]) for e in targets.values())
        total_calls = total_combos * 2
        print(f"Symbols selected for this run: {len(targets)}")
        print(f"  Truly-missing PNL quarter-combos: {total_combos}  ->  {total_calls} GetFinancialResults calls")
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

            year_periods_by_statement = {"ProfitLoss": sorted(entry["quarters"])}

            logger.info("[%s/%s] Fetching %s: PNL quarters=%s", idx, total, symbol, sorted(entry["quarters"]))
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

"""
Targeted backfill of BalanceSheet/CashFlow (M12, yearly only) for exactly the
(symbol, year) combos flagged as missing in the "All Symbols" sheet of
standalone_consolidated_report_by_period.xlsx -- the
consolidated_missing_periods/standalone_missing_periods columns, "BS:<year>"
and "CF:<year>" entries only (PNL entries are ignored in this pass).

For each symbol, the union of BS years missing (Consolidated or Standalone)
and CF years missing (Consolidated or Standalone) is fetched once via
fetch_historical_financials() -- same GetFinancialResults call used by the
other historical scripts -- then stored with
store_detailed_financials_for_symbol() from
fetch_historical_financial_stock_details.py (same storage logic as
fetch_todays_financials_and_stock_details.py: Consolidated preferred,
Standalone as a per-metric fallback, writing into
balance_sheet_financials/cashflow_financials). Nothing here touches
pnl_financials or ProfitLoss.

Read-only on the source xlsx; writes to balance_sheet_financials/
cashflow_financials only (never to `symbols`).

Usage:
    python backfill_bs_cf_from_report.py --plan-only          # just print the call plan, no network calls
    python backfill_bs_cf_from_report.py --limit 3            # real run, first 3 symbols only
    python backfill_bs_cf_from_report.py --symbols AHCL,BSOFT
    python backfill_bs_cf_from_report.py                      # full run, all flagged symbols
"""
from __future__ import annotations

import argparse
import logging
import re
import time

import openpyxl
from dotenv import load_dotenv

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

REPORT_PATH = "standalone_consolidated_report_by_period.xlsx"
SHEET_NAME = "All Symbols"
BS_CF_PERIOD_RE = re.compile(r"^(BS|CF):(\d{4})$")

STATEMENT_KEY = {"BS": "BalanceSheet", "CF": "CashFlow"}


def _parse_periods(raw) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def load_missing_bs_cf_years(path: str = REPORT_PATH) -> dict[int, dict]:
    """
    {symbol_id: {"symbol": str, "BalanceSheet": [years...], "CashFlow": [years...]}}
    Years are the union of consolidated_missing_periods and
    standalone_missing_periods BS:/CF: entries for that symbol -- one
    GetFinancialResults call per year already fetches both natures, so a
    year missing in either column needs the same fetch.
    """
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

        years = {"BalanceSheet": set(), "CashFlow": set()}
        for token in _parse_periods(cons_missing) + _parse_periods(stan_missing):
            m = BS_CF_PERIOD_RE.match(token)
            if not m:
                continue
            stmt, year = m.groups()
            years[STATEMENT_KEY[stmt]].add(int(year))

        if years["BalanceSheet"] or years["CashFlow"]:
            out[symbol_id] = {
                "symbol": symbol,
                "BalanceSheet": sorted(years["BalanceSheet"]),
                "CashFlow": sorted(years["CashFlow"]),
            }
    return out


def build_call_plan(targets: dict[int, dict]) -> tuple[int, dict]:
    total_combos = 0
    breakdown = {"BalanceSheet": 0, "CashFlow": 0}
    for entry in targets.values():
        breakdown["BalanceSheet"] += len(entry["BalanceSheet"])
        breakdown["CashFlow"] += len(entry["CashFlow"])
        total_combos += len(entry["BalanceSheet"]) + len(entry["CashFlow"])
    total_calls = total_combos * 2  # Consolidated + Standalone per combo
    return total_calls, breakdown


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=REPORT_PATH, help="Path to the xlsx report.")
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--limit", type=int, help="Only process the first N flagged symbols (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols.")
    parser.add_argument("--plan-only", action="store_true", help="Print the call plan and exit, no network calls.")
    args = parser.parse_args()

    configure_logging("backfill_bs_cf_from_report")
    load_metric_mappings()  # fail fast if Detailed_page_keys.xlsx is missing/malformed

    all_targets = load_missing_bs_cf_years(args.report)
    print(f"Symbols flagged with a missing BS/CF year in the report: {len(all_targets)}")

    if args.symbols:
        requested = {s.strip() for s in args.symbols.split(",") if s.strip()}
        all_targets = {sid: e for sid, e in all_targets.items() if e["symbol"] in requested}

    items = list(all_targets.items())
    if args.limit:
        items = items[: args.limit]
    targets = dict(items)

    total_calls, breakdown = build_call_plan(targets)
    print(f"Symbols selected for this run: {len(targets)}")
    print(f"  BalanceSheet year-combos: {breakdown['BalanceSheet']}, CashFlow year-combos: {breakdown['CashFlow']}")
    print(f"  Total GetFinancialResults calls (combos x 2 natures): {total_calls}")
    print(f"  GDFL account rate limit: {DEFAULT_RATE_LIMIT_PER_HOUR} requests/hour")
    if total_calls > DEFAULT_RATE_LIMIT_PER_HOUR:
        hours = -(-total_calls // DEFAULT_RATE_LIMIT_PER_HOUR)
        print(f"  WARNING: exceeds the hourly cap -- expect >= {hours} hour(s) wall-clock from rate-limit sleeps alone.")

    if args.plan_only:
        return

    db = SessionLocal()
    try:
        db_symbols = {
            s.id: s for s in db.query(Symbol).filter(Symbol.id.in_(targets.keys())).all()
        }

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        summary = {"symbols_processed": 0, "rows_written": 0}

        total = len(targets)
        for idx, (symbol_id, entry) in enumerate(targets.items(), start=1):
            symbol = entry["symbol"]
            db_symbol = db_symbols.get(symbol_id)
            if db_symbol is None or db_symbol.symbol != symbol:
                rejected_symbols.setdefault(symbol, []).append(
                    f"symbol_id {symbol_id} not found or symbol name mismatch in `symbols` table, skipped"
                )
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
                symbol,
                exchange=DEFAULT_EXCHANGE,
                year_periods_by_statement=year_periods_by_statement,
                client=client,
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

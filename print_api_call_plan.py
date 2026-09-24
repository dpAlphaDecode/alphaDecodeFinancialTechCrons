"""
Dry-run call-plan report for fetch_historical_financials_and_stock_details.py
-- prints every GDFL API call (endpoint + params) a real run would make, and
how many times each distinct params-shape fires, WITHOUT calling GDFLClient
or hitting the network, so it's safe to run anytime regardless of rate limit.

fetch_historical_financials_and_stock_details.py hits exactly one GDFL
endpoint -- GetFinancialResults (GDFLClient.get_financial_results) -- via
fetch_historical_financials() in fetch_historical_financials.py, once per
(statement_type, year, period, nature_of_report) combo per symbol. The
(year, period) combos are per-statement-type (STATEMENT_YEAR_PERIODS):
    ProfitLoss:               FY2026-27 QM, FY2026-27 QJ, FY2025-26 M12 -> 3 combos
    BalanceSheet / CashFlow:  FY2025-26 M12 only                        -> 1 combo each
each combo fetched for both natures (Consolidated, Standalone), so per
symbol: (3 + 1 + 1) combos * 2 natures = 10 GetFinancialResults calls. This
is the only endpoint either store path (store_financials_for_symbol /
store_detailed_financials_for_symbol) reads from -- both consume the same
fetched response, which is the whole point of the combined script.

Accepts the same --symbols/--exchange/--limit selection as
fetch_historical_financials_and_stock_details.py so the count matches
whatever you're about to actually run.

Usage:
    python print_api_call_plan.py
    python print_api_call_plan.py --symbols RELIANCE,TCS
    python print_api_call_plan.py --limit 50
"""
from __future__ import annotations

import argparse
from collections import Counter

from dotenv import load_dotenv

from db.session import SessionLocal
from fetch_historical_financials import (
    DEFAULT_EXCHANGE,
    NATURES_OF_REPORT,
    STATEMENT_TYPES,
    STATEMENT_YEAR_PERIODS,
    fetch_all_symbols,
)
from gdfl.client import DEFAULT_RATE_LIMIT_PER_HOUR

load_dotenv()

ENDPOINT = "GetFinancialResults"


def main():
    parser = argparse.ArgumentParser(
        description="Print the GDFL API call plan (endpoint/params/count) for a run of "
        "fetch_historical_financials_and_stock_details.py, without hitting the API."
    )
    parser.add_argument("--symbols", help="Comma-separated symbol list (same as the real script).")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--limit", type=int, help="Only consider the first N symbols.")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        db = SessionLocal()
        try:
            symbols = [s.symbol for s in fetch_all_symbols()]
        finally:
            db.close()

    if args.limit:
        symbols = symbols[: args.limit]

    combos_per_symbol = sum(len(STATEMENT_YEAR_PERIODS.get(stmt_type, [])) for stmt_type in STATEMENT_TYPES)
    per_symbol = combos_per_symbol * len(NATURES_OF_REPORT)
    total = per_symbol * len(symbols)

    print(f"{len(symbols)} symbol(s) selected (exchange={args.exchange}).")
    combo_breakdown = ", ".join(
        f"{stmt_type}={len(STATEMENT_YEAR_PERIODS.get(stmt_type, []))}" for stmt_type in STATEMENT_TYPES
    )
    print(
        f"Per symbol: {combos_per_symbol} (statement_type, year, period) combo(s) [{combo_breakdown}] "
        f"x {len(NATURES_OF_REPORT)} nature(s) = {per_symbol} {ENDPOINT} call(s)."
    )
    print(f"\nTotal {ENDPOINT} calls for this run: {total}\n")

    # Every (statement_type, year, period, nature) combo fires once per
    # symbol, so print the params *shape* with its count (= number of
    # symbols) rather than one line per symbol/call.
    shape_counts: Counter[tuple] = Counter()
    for stmt_type in STATEMENT_TYPES:
        for year, period in STATEMENT_YEAR_PERIODS.get(stmt_type, []):
            for nature in NATURES_OF_REPORT:
                shape_counts[(stmt_type, year, period, nature)] = len(symbols)

    print(f"{'endpoint':<20} {'type':<13} {'year':<6} {'period':<8} {'nature':<13} {'calls':>10}  params (exchange/instrument_identifier vary per symbol)")
    print("-" * 130)
    for (stmt_type, year, period, nature), count in shape_counts.items():
        params = f"exchange={args.exchange}, instrument_identifier=<symbol>, year={year}, period={period}, nature_of_report={nature}, type={stmt_type}"
        print(f"{ENDPOINT:<20} {stmt_type:<13} {year:<6} {period:<8} {nature:<13} {count:>10}  {params}")

    print(f"\n{'TOTAL':<20} {'':<13} {'':<6} {'':<8} {'':<13} {total:>10}")

    print(f"\nGDFL account rate limit: {DEFAULT_RATE_LIMIT_PER_HOUR} requests/hour (gdfl/client.py DEFAULT_RATE_LIMIT_PER_HOUR).")
    if total > DEFAULT_RATE_LIMIT_PER_HOUR:
        hours = -(-total // DEFAULT_RATE_LIMIT_PER_HOUR)  # ceil
        print(
            f"WARNING: {total} calls exceeds the hourly cap -- GDFLClient's rate limiter will "
            f"block/throttle mid-run; expect this run to take >= {hours} hour(s) wall-clock just "
            f"from rate-limit sleeps."
        )


if __name__ == "__main__":
    main()

"""
Combined historical financials + stock-details fetch/store job.

fetch_historical_financials.py (writes `financials`, Consolidated-priority)
and fetch_historical_financial_stock_details.py (writes pnl_financials/
balance_sheet_financials/cashflow_financials, Consolidated-first with a
per-metric Standalone fallback) each independently call
fetch_historical_financials() -- i.e. each hits GetFinancialResults for the
same (statement type, year, period, nature) combos, for the same symbols.
Running them as two separate cron jobs means GetFinancialResults gets hit
twice per symbol for identical data, which burns through GDFL's
1800-requests/hour cap for no reason.

This script fetches once per symbol and feeds both store paths from that
same response:
  - `financials` gets each metric's Consolidated value, falling back to
    Standalone only where Consolidated doesn't have it
    (store_financials_for_symbol() / extract_metrics_with_priority()).
  - pnl_financials/balance_sheet_financials/cashflow_financials get both
    Consolidated and Standalone considered per metric, Consolidated
    preferred with Standalone as the per-metric fallback
    (store_detailed_financials_for_symbol() / extract_detailed_metrics()).

Both store functions are unchanged -- this only removes the duplicate
GetFinancialResults calls by sharing one fetch between them.

Usage:
    python fetch_historical_financials_and_stock_details.py
    python fetch_historical_financials_and_stock_details.py --symbols RELIANCE,TCS
    python fetch_historical_financials_and_stock_details.py --json-dir out --limit 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from dotenv import load_dotenv

from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import (
    DEFAULT_EXCHANGE,
    fetch_all_symbols,
    fetch_historical_financials,
    store_financials_for_symbol,
)
from fetch_historical_financial_stock_details import (
    load_metric_mappings,
    store_detailed_financials_for_symbol,
)
from gdfl import GDFLClient

load_dotenv()

PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fetch_historical_financials_and_stock_details_progress.json")


def load_progress() -> dict:
    if not os.path.exists(PROGRESS_FILE):
        return {}
    try:
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_progress(index: int, total: int, symbol: str) -> None:
    with open(PROGRESS_FILE, "w") as f:
        json.dump(
            {
                "last_index": index,
                "total": total,
                "last_symbol": symbol,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            f,
            indent=2,
        )


def prompt_start_index(default_index: int) -> int:
    prompt = f"Start from which index? [default: {default_index}]: "
    try:
        raw = input(prompt).strip()
    except EOFError:
        return default_index
    if not raw:
        return default_index
    try:
        return int(raw)
    except ValueError:
        print(f"Invalid index '{raw}', using default {default_index}.")
        return default_index


def main():
    parser = argparse.ArgumentParser(
        description="Fetch GetFinancialResults once per symbol and store into both `financials` "
        "(Consolidated-priority) and pnl_financials/balance_sheet_financials/cashflow_financials "
        "(Consolidated+Standalone per metric)."
    )
    parser.add_argument("--symbols", help="Comma-separated symbol list to use instead of the DB (for testing).")
    parser.add_argument("--exchange", help="Override exchange for every symbol (default: BSE).")
    parser.add_argument(
        "--json-dir",
        help="Directory to also write one <symbol>.json file per symbol with the raw fetched data.",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N symbols (for testing).")
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    parser.add_argument(
        "--start-index",
        type=int,
        help="1-based index in the symbol list to start from, skipping the interactive prompt "
        "(use this for cron/non-interactive runs).",
    )
    args = parser.parse_args()

    load_metric_mappings()  # fail fast if Detailed_page_keys.xlsx is missing/malformed, before hitting the API

    db = SessionLocal()
    try:
        if args.symbols:
            requested = [s.strip() for s in args.symbols.split(",") if s.strip()]
            db_symbols = {
                s.symbol: s for s in db.query(Symbol).filter(Symbol.symbol.in_(requested)).all()
            }
            targets = [
                (sym, args.exchange or DEFAULT_EXCHANGE, db_symbols[sym].id if sym in db_symbols else None)
                for sym in requested
            ]
        else:
            symbols = fetch_all_symbols()
            targets = [(s.symbol, args.exchange or DEFAULT_EXCHANGE, s.id) for s in symbols]

        total = len(targets)

        progress = load_progress()
        default_start_index = progress.get("last_index", 0) + 1 if progress else 1
        default_start_index = min(default_start_index, total) if total else 1

        if args.start_index is not None:
            start_index = args.start_index
        elif sys.stdin.isatty():
            if progress.get("last_symbol"):
                print(f"Last run stopped at index {progress['last_index']} ({progress['last_symbol']}).")
            start_index = prompt_start_index(default_start_index)
        else:
            start_index = default_start_index

        start_index = max(1, start_index)
        targets = targets[start_index - 1 :]

        if args.limit:
            targets = targets[: args.limit]

        if args.json_dir:
            os.makedirs(args.json_dir, exist_ok=True)

        client = GDFLClient()
        rejected_symbols: dict[str, list[str]] = {}
        for idx, (symbol, exchange, symbol_id) in enumerate(targets, start=start_index):
            print(f"[{idx}/{total}] Fetching {symbol} ({exchange})...")

            if symbol_id is None:
                rejected_symbols.setdefault(symbol, []).append("symbol not found in `symbols` table, skipped")
                save_progress(idx, total, symbol)
                continue

            data = fetch_historical_financials(symbol, exchange=exchange, client=client)

            if args.json_dir:
                out_path = os.path.join(args.json_dir, f"{symbol}.json")
                with open(out_path, "w") as f:
                    json.dump(data, f, indent=2)

            written_financials = store_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            written_detailed = store_detailed_financials_for_symbol(db, symbol_id, symbol, data, rejected_symbols)
            db.commit()
            save_progress(idx, total, symbol)
            print(
                f"  stored/updated {written_financials} financials row(s), "
                f"{written_detailed} detailed metric row(s)"
            )

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

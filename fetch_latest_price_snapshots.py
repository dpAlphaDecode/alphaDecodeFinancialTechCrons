"""
Incremental OHLCV updater: for every symbol, finds the latest `date` already
stored in `price_snapshots`, then fetches just the missing days -- from the
day after that latest date through yesterday (today-1, IST) -- via GDFL's
GetHistory REST endpoint. A symbol with no price_snapshots rows yet falls
back to a trailing --lookback-days window (default 200) ending yesterday,
same as fetch_price_snapshots.py's default window.

Unlike fetch_price_snapshots.py (which re-fetches a fixed trailing window
for every symbol on every run), this only asks GDFL for the days actually
missing per symbol, so a symbol that's already up to date through yesterday
is skipped entirely (no API call).

Same GetHistory REST endpoint, GDFL_ACCESS_KEY, and insertion logic as
fetch_price_snapshots.py -- API_URL/USER_TAG/get_unix_timestamp/
insert_price_snapshots() are reused unchanged from there (same base URL,
same access key, same "only insert dates not already present" semantics),
not duplicated here.

Usage:
    python fetch_latest_price_snapshots.py
    python fetch_latest_price_snapshots.py --status ACCEPTED --exchange NSE
    python fetch_latest_price_snapshots.py --symbols RELIANCE,TCS
    python fetch_latest_price_snapshots.py --limit 500 --sleep 0.1
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.orm import Session

import requests

from db.models.price_snapshots import PriceSnapshot
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_price_snapshots import API_URL, USER_TAG, get_unix_timestamp, insert_price_snapshots
from fetch_results_calendar import IST
from logging_setup import configure_logging

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_EXCHANGE = "NSE"
# Fallback trailing window for a symbol with no price_snapshots rows yet --
# matches fetch_price_snapshots.py's DEFAULT_DAYS.
DEFAULT_LOOKBACK_DAYS = 200


def fetch_history_range(symbol_code: str, exchange: str, from_date: date, to_date: date, api_key: str) -> list[dict]:
    """
    Same GetHistory call as fetch_price_snapshots.fetch_history(), but over
    an explicit [from_date, to_date] range instead of a trailing N-day
    window. Naive (local-time) datetimes are used for the from/to
    timestamps, matching fetch_history()'s own datetime.now() (no tzinfo)
    exactly -- only the calendar date boundaries themselves (which dates
    count as "missing"/"yesterday") are computed in IST elsewhere.
    """
    from_ts = get_unix_timestamp(datetime.combine(from_date, datetime.min.time()))
    to_ts = get_unix_timestamp(datetime.combine(to_date, datetime.max.time()))
    params = {
        "accessKey": api_key,
        "exchange": exchange,
        "instrumentIdentifier": symbol_code,
        "periodicity": "DAY",
        "period": 1,
        "from": from_ts,
        "to": to_ts,
        "max": 0,
        "userTag": USER_TAG,
    }
    try:
        response = requests.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json().get("OHLC", [])
    except Exception:
        logger.exception("Error fetching history range for %s (%s -> %s)", symbol_code, from_date, to_date)
        return []


def latest_dates_by_symbol(db: Session) -> dict[int, date]:
    """{symbol_id: latest `date` already in price_snapshots} -- symbols with no rows are simply absent."""
    rows = (
        db.query(PriceSnapshot.symbol_id, func.max(PriceSnapshot.date))
        .group_by(PriceSnapshot.symbol_id)
        .all()
    )
    return {symbol_id: latest for symbol_id, latest in rows}


def run_incremental_update(
    status: str | None,
    exchange: str,
    limit: int | None,
    sleep: float,
    api_key: str,
    symbols_filter: set[str] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
    db: Session | None = None,
) -> dict:
    """
    `db` lets a caller pass in a session it already owns; when omitted,
    this opens and closes its own. Returns a summary dict.

    `dry_run=True` still calls the real GetHistory API (there's no other way
    to know what's missing) but rolls back instead of committing, so nothing
    is actually written to `price_snapshots`.
    """
    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    summary = {
        "symbols_found": 0,
        "symbols_up_to_date": 0,
        "symbols_processed": 0,
        "symbols_skipped_no_data": 0,
        "rows_inserted": 0,
    }
    try:
        query = db.query(Symbol)
        if status:
            query = query.filter(Symbol.final_status == status)
        if symbols_filter:
            query = query.filter(Symbol.symbol.in_(symbols_filter))
        query = query.order_by(Symbol.id.asc())
        if limit:
            query = query.limit(limit)
        symbols = query.all()
        summary["symbols_found"] = len(symbols)

        latest_by_symbol = latest_dates_by_symbol(db)
        yesterday = (datetime.now(IST) - timedelta(days=1)).date()

        prefix = "[DRY RUN] " if dry_run else ""
        logger.info("%sFound %s symbol(s); filling OHLCV gaps up to %s (today-1, IST)", prefix, len(symbols), yesterday)

        for idx, sym in enumerate(symbols, start=1):
            latest = latest_by_symbol.get(sym.id)
            from_date = latest + timedelta(days=1) if latest is not None else yesterday - timedelta(days=lookback_days)

            if from_date > yesterday:
                logger.info("%s[%s/%s] %s already up to date (latest=%s)", prefix, idx, len(symbols), sym.symbol, latest)
                summary["symbols_up_to_date"] += 1
                continue

            logger.info(
                "%s[%s/%s] %s: fetching %s -> %s (latest stored: %s)",
                prefix, idx, len(symbols), sym.symbol, from_date, yesterday, latest,
            )
            ohlc_data = fetch_history_range(sym.symbol, exchange, from_date, yesterday, api_key)
            if not ohlc_data:
                logger.warning("%sSkipping %s, no data for %s -> %s", prefix, sym.symbol, from_date, yesterday)
                summary["symbols_skipped_no_data"] += 1
                continue

            added = insert_price_snapshots(sym.id, ohlc_data, db)
            dates_fetched = sorted(
                datetime.utcfromtimestamp(row["LASTTRADETIME"] // 1000).date().isoformat() for row in ohlc_data
            )
            if dry_run:
                db.rollback()
            else:
                db.commit()
            verb = "would insert" if dry_run else "inserted"
            logger.info(
                "%s%s %s new record(s) for %s (dates in response: %s)",
                prefix, verb, added, sym.symbol, dates_fetched,
            )
            summary["symbols_processed"] += 1
            summary["rows_inserted"] += added

            if sleep and idx < len(symbols):
                time.sleep(sleep)
    except Exception as e:
        logger.exception("Incremental price snapshot update failed")
        summary["error"] = str(e)
    finally:
        if owns_db:
            db.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="For every symbol, fetch OHLCV (GDFL GetHistory) from the day after its latest "
        "price_snapshots row through yesterday (today-1, IST), and insert only the missing dates."
    )
    parser.add_argument("--status", default="", help="Symbol.final_status filter (default: no filter, i.e. all symbols).")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--symbols", help="Comma-separated symbol list to restrict to (for testing).")
    parser.add_argument("--limit", type=int, default=None, help="Max number of symbols to process.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="Trailing window to use for a symbol with no price_snapshots rows yet.",
    )
    parser.add_argument("--sleep", type=float, default=0.0, help="Seconds to sleep between symbols (rate limiting).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Still call GetHistory for real, but roll back instead of committing -- nothing is written.",
    )
    args = parser.parse_args()

    configure_logging("fetch_latest_price_snapshots")
    api_key = os.environ.get("GDFL_ACCESS_KEY")
    if not api_key:
        raise SystemExit("GDFL_ACCESS_KEY is not set (.env or environment).")

    symbols_filter = {s.strip() for s in args.symbols.split(",") if s.strip()} if args.symbols else None

    summary = run_incremental_update(
        status=args.status or None,
        exchange=args.exchange,
        limit=args.limit,
        sleep=args.sleep,
        api_key=api_key,
        symbols_filter=symbols_filter,
        lookback_days=args.lookback_days,
        dry_run=args.dry_run,
    )
    logger.info("%sDone: %s", "[DRY RUN] " if args.dry_run else "", summary)


if __name__ == "__main__":
    main()

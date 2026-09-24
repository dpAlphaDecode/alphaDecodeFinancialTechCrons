"""
Fetches daily EOD OHLCV via GDFL's GetHistory REST endpoint
(https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/rest-api-documentation/function-gethistory/)
and inserts it into the `price_snapshots` table (db/models/price_snapshots.py),
one row per (symbol_id, date).

Confirmed live response shape (2026-09-24): {"USERTAG": ..., "OHLC": [
{"OPEN":.., "HIGH":.., "LOW":.., "CLOSE":.., "TRADEDQTY":..,
"LASTTRADETIME": <epoch ms>, ...}, ...]}. See gdfl/client.py's
GDFLClient.get_history() for the same endpoint wrapped with this repo's
_get()/rate-limiting/error-handling plumbing -- this script calls the REST
API directly instead (matching the logic given for this script), which
means it does NOT get GDFLClient's 1800-req/hour throttling or retry/error
wrapping; add --sleep if you hit rate limits.

Per symbol: fetches the trailing --days (default 200) calendar days every
run, then inserts only the dates not already present for that symbol_id --
existing rows are left untouched (never updated), so re-running is safe but
won't correct a previously-stored bad value for a date that's already there.

ACCOUNT KEY NOTE: the access key must be set via GDFL_ACCESS_KEY (.env) --
never hardcode a key in this file. Two issues seen against this account
while building this:
  - the key in .env as of 2026-09-24 gets "Data for requested exchange is
    disabled" for exchange=NSE (only BSE worked).
  - a second key tried had expired ("Key Expired").
Confirm GDFL_ACCESS_KEY in .env is a currently-valid key with NSE enabled
before running this for real.

Usage:
    python fetch_price_snapshots.py
    python fetch_price_snapshots.py --status ACCEPTED --id-start 5000 --id-end 6000
    python fetch_price_snapshots.py --limit 1000 --days 200 --sleep 0.1
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from db.models.price_snapshots import PriceSnapshot
from db.models.symbol import Symbol
from db.session import SessionLocal

load_dotenv()

API_URL = "https://nimblerest.lisuns.com:4532/GetHistory"
DEFAULT_DAYS = 200
DEFAULT_EXCHANGE = "NSE"
USER_TAG = "decodeAlpha"


def get_unix_timestamp(dt: datetime) -> int:
    return int(dt.timestamp())


def fetch_history(symbol_code: str, exchange: str, days: int, api_key: str) -> list[dict]:
    to_ts = get_unix_timestamp(datetime.now())
    from_ts = get_unix_timestamp(datetime.now() - timedelta(days=days))
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
    except Exception as e:
        print(f"❌ Error for {symbol_code}: {e}")
        return []


def insert_price_snapshots(symbol_id: int, ohlc: list[dict], db: Session) -> int:
    # Fetch all existing dates for this symbol
    existing_dates = set(
        r[0] for r in db.query(PriceSnapshot.date)
                        .filter(PriceSnapshot.symbol_id == symbol_id)
                        .all()
    )
    to_insert = []
    for row in ohlc:
        d = datetime.utcfromtimestamp(row["LASTTRADETIME"] // 1000).date()
        if d in existing_dates:
            continue
        to_insert.append(PriceSnapshot(
            symbol_id=symbol_id,
            date=d,
            open_price=row.get("OPEN"),
            close_price=row.get("CLOSE"),
            high=row.get("HIGH"),
            low=row.get("LOW"),
            volume=row.get("TRADEDQTY", 0),
        ))
        existing_dates.add(d)  # Prevent re-insertion in this batch

    if to_insert:
        db.bulk_save_objects(to_insert)
    return len(to_insert)


def run_snapshot_scraper(
    status: str | None,
    id_start: int | None,
    id_end: int | None,
    limit: int | None,
    days: int,
    exchange: str,
    api_key: str,
    db: Session | None = None,
) -> dict:
    """
    `db` lets a caller (e.g. the scheduler runner) pass in a session it already
    owns; when omitted, this opens and closes its own. Returns a summary dict
    for callers (e.g. scheduler/jobs.py) that need more than the printed log.
    """
    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    summary = {"symbols_found": 0, "symbols_processed": 0, "symbols_skipped_no_data": 0, "rows_inserted": 0}
    try:
        query = db.query(Symbol)
        if status:
            query = query.filter(Symbol.final_status == status)
        if id_start is not None and id_end is not None:
            query = query.filter(Symbol.id.between(id_start, id_end))
        query = query.order_by(Symbol.id.asc())
        if limit:
            query = query.limit(limit)
        symbols = query.all()
        summary["symbols_found"] = len(symbols)

        print(f"\U0001f50d Found {len(symbols)} symbols to process")
        for sym in symbols:
            print(f"\U0001f4ca Processing {sym.symbol} (ID: {sym.id})")
            ohlc_data = fetch_history(sym.symbol, exchange, days, api_key)
            print(f"\U0001f4c8 Fetched {len(ohlc_data)} records for {sym.symbol}")
            if not ohlc_data:
                print(f"⚠️ Skipping {sym.symbol}, no data")
                summary["symbols_skipped_no_data"] += 1
                continue
            added = insert_price_snapshots(sym.id, ohlc_data, db)
            db.commit()
            print(f"✅ Inserted {added} new records for {sym.symbol}")
            summary["symbols_processed"] += 1
            summary["rows_inserted"] += added
    except Exception as e:
        print(f"❌ Scraper failed: {e}")
        summary["error"] = str(e)
    finally:
        if owns_db:
            db.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Fetch daily EOD OHLCV via GDFL's GetHistory REST endpoint and insert into price_snapshots."
    )
    parser.add_argument("--status", default="ACCEPTED", help="Symbol.final_status filter (pass '' to disable).")
    parser.add_argument("--id-start", type=int, default=None, help="Only symbols with id >= this.")
    parser.add_argument("--id-end", type=int, default=None, help="Only symbols with id <= this (requires --id-start).")
    parser.add_argument("--limit", type=int, default=None, help="Max number of symbols to process.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Trailing calendar-day window to fetch per symbol.")
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    args = parser.parse_args()

    api_key = os.environ.get("GDFL_ACCESS_KEY")
    if not api_key:
        raise SystemExit("GDFL_ACCESS_KEY is not set (.env or environment).")

    if (args.id_start is None) != (args.id_end is None):
        raise SystemExit("--id-start and --id-end must be given together.")

    run_snapshot_scraper(
        status=args.status or None,
        id_start=args.id_start,
        id_end=args.id_end,
        limit=args.limit,
        days=args.days,
        exchange=args.exchange,
        api_key=api_key,
    )


if __name__ == "__main__":
    main()

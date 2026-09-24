"""
Fetch upcoming/recent financial-results announcement dates from GDFL's
GetResultsCalendar API, then for each matched company use
GetFinancialResultsItems to find every (NatureOfReport, ResultsType,
ResultPeriod) filing actually available for that company's FY, and upsert
one `financial_calendar` row per (symbol, result_date, results_type,
results_period, nature_of_report) -- history is kept, not overwritten.

GetResultsCalendar returns a batch of companies for a given exchange/range
in one call (not filtered by instrument), each with an ISIN but no direct
key into our `symbols` table, so rows are matched by ISIN. Companies whose
ISIN isn't found in `symbols` are skipped and reported at the end.

`ResultsDate` (and `ReceivedAt`/`SavedAt`) are epoch-millisecond UTC
timestamps that encode an IST calendar date at IST midnight (e.g.
2026-09-20T18:30:00Z == 2026-09-21T00:00:00+05:30) -- this converts them to
IST before taking the date.

GetFinancialResultsItems' `period` param is optional on the live server
(despite the client wrapper's type hint) -- omitting it returns every
period available for the given `year`, which is what GDFL's FY-start-year
convention (see `current_fy_year`) derived from each result_date maps to.

Usage:
    python fetch_results_calendar.py
    python fetch_results_calendar.py --range Next7days,Previous7days
    python fetch_results_calendar.py --exchange BSE --range Next30days
"""
from __future__ import annotations

import argparse
import datetime as _dt

from dotenv import load_dotenv

from db.models.financial_calendar import FinancialCalendar
from db.models.symbol import Symbol
from db.session import SessionLocal
from fetch_historical_financials import current_fy_year
from gdfl import GDFLAPIError, GDFLClient

load_dotenv()

DEFAULT_EXCHANGE = "BSE"
DEFAULT_RANGES = ["Next30days"]
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _results_date_to_ist_date(epoch_ms) -> _dt.date | None:
    if epoch_ms is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(int(epoch_ms) / 1000, tz=IST).date()
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_results_calendar_entries(
    exchange: str = DEFAULT_EXCHANGE,
    ranges: tuple[str, ...] = DEFAULT_RANGES,
    client: GDFLClient | None = None,
) -> list[dict]:
    """Fetches every range and dedupes entries by (ISIN, ResultsDate)."""
    client = client or GDFLClient()
    seen: dict[tuple[str, int], dict] = {}
    for range_ in ranges:
        try:
            response = client.get_results_calendar(exchange=exchange, range=range_)
        except GDFLAPIError as exc:
            print(f"  GetResultsCalendar range={range_} failed: {exc}")
            continue
        for entry in response.get("Value", []):
            isin = entry.get("ISIN")
            results_date = entry.get("ResultsDate")
            if not isin or results_date is None:
                continue
            seen[(isin, results_date)] = entry
    return list(seen.values())


def fetch_financial_results_items(
    client: GDFLClient, exchange: str, scrip_id: str, year: int
) -> list[dict]:
    """All (NatureOfReport, ResultsType, ResultPeriod) combos GDFL has on file for this instrument/year."""
    try:
        response = client.get_financial_results_items(
            year=year, period=None, exchange=exchange, instrument_identifiers=scrip_id
        )
    except GDFLAPIError as exc:
        print(f"  GetFinancialResultsItems failed for {scrip_id} year={year}: {exc}")
        return []
    return response.get("Value", [])


def store_results_calendar(
    db, entries: list[dict], exchange: str, client: GDFLClient
) -> tuple[int, list[str]]:
    """
    For each calendar entry, matches `symbols` by ISIN, then looks up every
    filing combo available for that company's FY via GetFinancialResultsItems
    and upserts one `financial_calendar` row per combo. Returns (rows
    written, unmatched/skipped entries).
    """
    isins = {e["ISIN"] for e in entries if e.get("ISIN")}
    symbol_id_by_isin = {
        s.isin: s.id for s in db.query(Symbol).filter(Symbol.isin.in_(isins)).all()
    }

    written = 0
    unmatched = []
    items_cache: dict[tuple[str, int], list[dict]] = {}
    for entry in entries:
        isin = entry.get("ISIN")
        scrip_id = entry.get("ScripId")
        symbol_id = symbol_id_by_isin.get(isin)
        if symbol_id is None:
            unmatched.append(f"{scrip_id} ({isin}) not found in symbols table")
            continue

        result_date = _results_date_to_ist_date(entry.get("ResultsDate"))
        if result_date is None:
            unmatched.append(f"{scrip_id} ({isin}) has no usable ResultsDate")
            continue

        year = current_fy_year(result_date)
        cache_key = (scrip_id, year)
        if cache_key not in items_cache:
            items_cache[cache_key] = fetch_financial_results_items(client, exchange, scrip_id, year)
        items = items_cache[cache_key]
        if not items:
            unmatched.append(f"{scrip_id} ({isin}) no GetFinancialResultsItems rows for year={year}")
            continue

        for item in items:
            results_type = item.get("ResultsType")
            results_period = item.get("ResultPeriod")
            nature_of_report = item.get("NatureOfReport")
            row = (
                db.query(FinancialCalendar)
                .filter_by(
                    symbol_id=symbol_id,
                    result_date=result_date,
                    results_type=results_type,
                    results_period=results_period,
                    nature_of_report=nature_of_report,
                )
                .first()
            )
            if row is None:
                row = FinancialCalendar(
                    symbol_id=symbol_id,
                    result_date=result_date,
                    results_type=results_type,
                    results_period=results_period,
                    nature_of_report=nature_of_report,
                )
                db.add(row)
            row.result_year = item.get("Year")
            db.flush()
            written += 1
    return written, unmatched


def run(
    exchange: str = DEFAULT_EXCHANGE,
    ranges: tuple[str, ...] = tuple(DEFAULT_RANGES),
    db=None,
) -> dict:
    """
    Callable form of main()'s logic, for use by the scheduler (scheduler/jobs.py)
    as well as the CLI below.

    `db` lets a caller (e.g. the scheduler runner) pass in a session it already
    owns; when omitted, this opens and closes its own.
    """
    client = GDFLClient()
    print(f"Fetching results calendar for {exchange} ranges={ranges}...")
    entries = fetch_results_calendar_entries(exchange=exchange, ranges=ranges, client=client)
    print(f"Fetched {len(entries)} unique (ISIN, ResultsDate) entries.")

    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        written, unmatched = store_results_calendar(db, entries, exchange, client)
        db.commit()
    finally:
        if owns_db:
            db.close()

    print(f"Stored/updated {written} rows.")
    if unmatched:
        print("\n===== Unmatched entries =====")
        for line in unmatched:
            print(f"  {line}")

    return {"entries_fetched": len(entries), "written": written, "unmatched": unmatched}


def main():
    parser = argparse.ArgumentParser(
        description="Fetch results-calendar dates from GDFL and upsert them into financial_calendar."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument(
        "--range",
        default=",".join(DEFAULT_RANGES),
        help="Comma-separated GDFL range values, e.g. Today,Next7days,Previous7days,Next30days,Previous30days.",
    )
    args = parser.parse_args()
    ranges = tuple(r.strip() for r in args.range.split(",") if r.strip())

    run(exchange=args.exchange, ranges=ranges)


if __name__ == "__main__":
    main()

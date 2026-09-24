"""
Builds a symbols-master .xlsx from GDFL's GetInstruments API -- one call per
target series returns every currently-active instrument in that series for
the exchange, so this is a one-shot snapshot of the current listed universe
rather than a daily historical pull.

Response rows (per gdfl/client.py's GDFLClient.get_instruments(), verified
live against nimblerest.lisuns.com:4532) use UPPERCASE fields wrapped in
{"INSTRUMENTS": [...]}: `IDENTIFIER` (ticker symbol -- matches what the rest
of this codebase passes as `instrument_identifier` elsewhere), `DESCRIPTION`
(company name -- NOT `NAME`, which just repeats the symbol for equities),
`ISIN`, `SERIES`, and (with detailedInfo=true) `52WeekHigh`/`52WeekLow`.

Called once per series in TARGET_SERIES (GetInstruments' own `series` param
filters server-side), with `onlyActive=true` and `showDummyISIN=false` so
placeholder/inactive instruments are already excluded. Rows are additionally
kept only if `ISIN` starts with "INE" (standard equity ISIN) or "IN"
followed by a digit (e.g. "IN0...", "IN46..." -- government-security-style
ISINs); "IN" followed by any other letter (e.g. "INF..." mutual funds,
"IND...") is rejected even though it shares the "IN" country prefix. Rows
are deduped by ISIN across the per-series calls.

Output columns match the `symbols` DB table (db/models/symbol.py) columns
this script can populate: symbol, name, exchange, isin, series,
high_52_week, low_52_week. The remaining columns there (logo_url,
listed_date, industry_id, sector_id, final_status, archive) aren't returned
by GetInstruments.

After writing the xlsx, every fetched row is also upserted into the
`symbols` table by matching on `symbol` and `isin` (see upsert_symbols()):
  - both `symbol` and `isin` match the same row -> already in sync, no change.
  - `symbol` matches a row but that row's `isin` differs -> update its isin.
  - `isin` matches a row but that row's `symbol` differs -> update its symbol.
  - `symbol` matches one row and `isin` matches a *different* row -> ambiguous
    data conflict, skipped (logged) rather than guessed at.
  - neither matches -> brand new symbol, inserted. Columns GetInstruments
    doesn't provide (logo_url, listed_date, industry_id, sector_id,
    final_status) are set to their type's empty value (`""`/`None`) rather
    than guessed at; high_52_week/low_52_week default to 0 when GetInstruments
    didn't return them either.

Usage:
    python fetch_bhavcopy_symbols.py
    python fetch_bhavcopy_symbols.py --exchange BSE --output bhavcopy_symbols.xlsx
"""
from __future__ import annotations

import pandas as pd
import argparse

from dotenv import load_dotenv

from db.models.symbol import Symbol
from db.session import SessionLocal
from gdfl import GDFLAPIError, GDFLClient

load_dotenv()

DEFAULT_EXCHANGE = "BSE"
DEFAULT_OUTPUT = "bhavcopy_symbols.xlsx"

# BSE Equity group/series codes to keep.
TARGET_SERIES = ["A", "B", "T", "Z", "ZP", "X", "XT", "P"]

# Output column order -- the `symbols` table columns this script can populate.
OUTPUT_COLUMNS = ["symbol", "name", "exchange", "isin", "series", "high_52_week", "low_52_week"]


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        value = float(str(value).strip())
    except ValueError:
        return None
    return value if value != 0 else None


def _matches_isin(row: dict) -> bool:
    """
    Keeps "INE..." (standard equity ISIN) and "IN" followed by a digit
    (e.g. "IN0...", "IN46..." -- government-security-style ISINs). Rejects
    "IN" followed by any other letter (e.g. "INF..." mutual funds,
    "IND...") -- those aren't equity/govt-security ISINs even though they
    share the "IN" country prefix.
    """
    isin = str(row.get("ISIN") or "").strip().upper()
    if isin.startswith("INE"):
        return True
    return len(isin) > 2 and isin[:2] == "IN" and isin[2].isdigit()


def build_symbol_rows(instrument_rows: list[dict], exchange: str) -> list[dict]:
    """Filters to a valid ISIN prefix and dedupes by ISIN (first occurrence wins)."""
    records: list[dict] = []
    seen_isins: set[str] = set()
    for row in instrument_rows:
        if not _matches_isin(row):
            continue
        isin = str(row.get("ISIN") or "").strip().upper()
        if isin in seen_isins:
            continue
        seen_isins.add(isin)
        records.append(
            {
                "symbol": str(row.get("IDENTIFIER") or "").strip(),
                "name": str(row.get("DESCRIPTION") or "").strip(),
                "exchange": exchange,
                "isin": isin,
                "series": str(row.get("SERIES") or "").strip().upper(),
                "high_52_week": _to_float(row.get("52WeekHigh")),
                "low_52_week": _to_float(row.get("52WeekLow")),
            }
        )
    return records


def load_existing_symbols(db) -> tuple[dict[str, Symbol], dict[str, Symbol]]:
    """Every `symbols` row, indexed by (uppercased) symbol and by isin for in-memory matching."""
    rows = db.query(Symbol).all()
    by_symbol = {row.symbol.strip().upper(): row for row in rows if row.symbol and row.symbol.strip()}
    by_isin = {row.isin.strip().upper(): row for row in rows if row.isin and row.isin.strip()}
    return by_symbol, by_isin


def upsert_symbols(db, records: list[dict], exchange: str) -> dict[str, int]:
    """
    Matches each fetched record against the `symbols` table on symbol and
    isin, updating the mismatched column when only one of the two matches,
    inserting a new row when neither matches, and skipping (with a logged
    warning) when symbol and isin each match a *different* existing row.
    """
    by_symbol, by_isin = load_existing_symbols(db)
    stats = {"inserted": 0, "isin_updated": 0, "symbol_updated": 0, "unchanged": 0, "conflicts": 0}

    for rec in records:
        symbol = (rec["symbol"] or "").strip().upper()
        isin = (rec["isin"] or "").strip().upper()
        if not symbol:
            continue

        row_by_symbol = by_symbol.get(symbol)
        row_by_isin = by_isin.get(isin) if isin else None

        if row_by_symbol is not None and row_by_isin is not None and row_by_symbol is not row_by_isin:
            print(
                f"  CONFLICT: symbol={symbol!r} belongs to row id={row_by_symbol.id} but "
                f"isin={isin!r} belongs to a different row id={row_by_isin.id}; skipping."
            )
            stats["conflicts"] += 1
            continue

        if row_by_symbol is not None:
            existing_isin = (row_by_symbol.isin or "").strip().upper()
            if isin and existing_isin != isin:
                row_by_symbol.isin = isin
                by_isin[isin] = row_by_symbol
                stats["isin_updated"] += 1
            else:
                stats["unchanged"] += 1
            continue

        if row_by_isin is not None:
            row_by_isin.symbol = symbol
            by_symbol[symbol] = row_by_isin
            stats["symbol_updated"] += 1
            continue

        new_row = Symbol(
            symbol=symbol,
            name=rec["name"] or "",
            exchange=exchange or "",
            isin=isin or None,
            series=rec["series"] or "",
            high_52_week=rec["high_52_week"] if rec["high_52_week"] is not None else 0,
            low_52_week=rec["low_52_week"] if rec["low_52_week"] is not None else 0,
            logo_url="",
            listed_date=None,
            industry_id=None,
            sector_id=None,
            final_status="",
            archive=False,
        )
        db.add(new_row)
        by_symbol[symbol] = new_row
        if isin:
            by_isin[isin] = new_row
        stats["inserted"] += 1

    db.commit()
    return stats


def fetch_instrument_rows(exchange: str, series_list: list[str], client: GDFLClient) -> list[dict]:
    """One GetInstruments call per series (server-side filtered), combined into a single list."""
    rows: list[dict] = []
    for series in series_list:
        response = client.get_instruments(
            exchange=exchange,
            series=series,
            only_active=True,
            show_dummy_isin=False,
            detailed_info=True,
        )
        series_rows = response.get("INSTRUMENTS", []) if isinstance(response, dict) else []
        print(f"  series={series}: {len(series_rows)} instrument(s)")
        rows.extend(series_rows)
    return rows


def run(
    exchange: str = DEFAULT_EXCHANGE,
    write_xlsx: bool = False,
    output: str = DEFAULT_OUTPUT,
    db=None,
) -> dict:
    """
    Callable form of main()'s logic, for use by the scheduler (scheduler/jobs.py)
    as well as the CLI below. Returns the upsert stats dict plus row counts.

    `db` lets a caller (e.g. the scheduler runner) pass in a session it already
    owns; when omitted, this opens and closes its own.
    """
    client = GDFLClient()
    print(f"Fetching GetInstruments for {exchange}, series={TARGET_SERIES}...")
    rows = fetch_instrument_rows(exchange, TARGET_SERIES, client)
    print(f"Fetched {len(rows)} instrument row(s) total.")

    records = build_symbol_rows(rows, exchange)
    print(f"{len(records)} row(s) matched ISIN prefix INE/IN after dedup.")

    if write_xlsx:
        df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
        df.to_excel(output, index=False)
        print(f"Wrote {len(df)} rows to {output}")

    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        stats = upsert_symbols(db, records, exchange)
    finally:
        if owns_db:
            db.close()

    stats["instrument_rows"] = len(rows)
    stats["matched_rows"] = len(records)
    print(
        f"DB upsert: {stats['inserted']} inserted, {stats['isin_updated']} isin updated, "
        f"{stats['symbol_updated']} symbol updated, {stats['unchanged']} unchanged, "
        f"{stats['conflicts']} conflict(s) skipped."
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Fetch GetInstruments, filter to target series/ISIN prefixes, and write a symbols-master xlsx."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output .xlsx path.")
    args = parser.parse_args()

    try:
        run(exchange=args.exchange, write_xlsx=True, output=args.output)
    except GDFLAPIError as exc:
        print(f"GetInstruments failed: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

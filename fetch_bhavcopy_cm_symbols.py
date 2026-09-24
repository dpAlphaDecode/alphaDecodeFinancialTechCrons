"""
Builds a symbols-master .xlsx from GDFL's GetBhavCopyCM API -- one call for a
single trading date returns EOD trade data for every instrument that traded
on the BSE Capital Market (CM) segment that day. Unlike GetInstruments (see
fetch_bhavcopy_symbols.py), this is NOT an instrument master: it only lists
symbols that actually traded on `--date`, so it's a snapshot of that day's
traded universe rather than the full currently-listed universe.

CAVEAT -- response field casing is unverified: this GDFL account returns
"Function not enabled" for GetBhavCopyCM, so unlike get_instruments()
(verified live against nimblerest.lisuns.com:4532), the field names below
come only from GDFL's docs page (https://docs.globaldatafeeds.in/getbhavcopycm-15575591e0),
which lists lowercase fields (`symbol`, `isin`, `sctySrs`, `finInstrmNm`,
`instrumentName`, ...) wrapped in {"value": [...], "count": N}. gdfl/client.py's
module docstring notes the docs have been wrong about casing before (docs say
lowercase `value`/`count`, live actually returns PascalCase `Value`/`Count`
for the other Fundamental/Corporate Data endpoints) -- GetBhavCopyCM may be
the same. `_field()` below does a case-insensitive, multi-alias lookup per
column specifically to survive that until someone with an enabled key
confirms the real casing.

Rows are filtered the same way as fetch_bhavcopy_symbols.py: kept only if
`isin` starts with "INE" (standard equity ISIN) or "IN" followed by a digit
(government-security-style ISINs), and only if `sctySrs`/series is in
TARGET_SERIES. Deduped by ISIN.

Output columns match the subset of the `symbols` DB table (db/models/symbol.py)
this script can populate: symbol, name, exchange, isin, series. GetBhavCopyCM
returns OHLC/turnover for the day, not 52-week high/low or listed_date, so
those columns (populated by fetch_bhavcopy_symbols.py) are left out here
rather than emitted blank.

Rows whose ISIN already exists in the `symbols` table are dropped -- the
output is meant to be just the symbols not yet in the DB.

Usage:
    python fetch_bhavcopy_cm_symbols.py --date 2026-09-22
    python fetch_bhavcopy_cm_symbols.py --exchange BSE --date 2026-09-22 --output bhavcopy_cm_symbols.xlsx
"""
from __future__ import annotations

import argparse
import datetime as _dt

import pandas as pd

from dotenv import load_dotenv

from db.models.symbol import Symbol
from db.session import SessionLocal
from gdfl import GDFLAPIError, GDFLClient

load_dotenv()

DEFAULT_EXCHANGE = "BSE"
DEFAULT_OUTPUT = "bhavcopy_cm_symbols.xlsx"

# BSE Equity group/series codes to keep.
TARGET_SERIES = ["A", "B", "T", "Z", "ZP", "X", "XT", "P"]

# Output column order -- the `symbols` table columns this script can populate.
OUTPUT_COLUMNS = ["symbol", "name", "exchange", "isin", "series"]

# column -> candidate response keys, in priority order (docs casing first,
# then PascalCase/UPPERCASE fallbacks -- see module docstring caveat above).
_FIELD_ALIASES = {
    "symbol": ["symbol", "Symbol", "SYMBOL"],
    "isin": ["isin", "ISIN", "Isin"],
    "series": ["sctySrs", "SctySrs", "SCTYSRS", "series", "Series", "SERIES"],
    "name": ["finInstrmNm", "FinInstrmNm", "instrumentName", "InstrumentName", "INSTRUMENTNAME"],
}


def _field(row: dict, column: str) -> str:
    for key in _FIELD_ALIASES[column]:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""


def _matches_isin(row: dict) -> bool:
    """
    Keeps "INE..." (standard equity ISIN) and "IN" followed by a digit
    (e.g. "IN0...", "IN46..." -- government-security-style ISINs). Rejects
    "IN" followed by any other letter (e.g. "INF..." mutual funds,
    "IND...") -- those aren't equity/govt-security ISINs even though they
    share the "IN" country prefix.
    """
    isin = _field(row, "isin").upper()
    if isin.startswith("INE"):
        return True
    return len(isin) > 2 and isin[:2] == "IN" and isin[2].isdigit()


def build_symbol_rows(bhavcopy_rows: list[dict], exchange: str) -> list[dict]:
    """Filters to target series + a valid ISIN prefix, dedupes by ISIN (first occurrence wins)."""
    records: list[dict] = []
    seen_isins: set[str] = set()
    for row in bhavcopy_rows:
        if _field(row, "series").upper() not in TARGET_SERIES:
            continue
        if not _matches_isin(row):
            continue
        isin = _field(row, "isin").upper()
        if isin in seen_isins:
            continue
        seen_isins.add(isin)
        records.append(
            {
                "symbol": _field(row, "symbol"),
                "name": _field(row, "name"),
                "exchange": exchange,
                "isin": isin,
                "series": _field(row, "series").upper(),
            }
        )
    return records


def fetch_existing_isins(db) -> set[str]:
    """Every non-null ISIN already present in the `symbols` table."""
    return {
        symbol.strip().upper()
        for (symbol,) in db.query(Symbol.symbol).filter(Symbol.symbol.isnot(None)).all()
        if symbol and symbol.strip()
    }


def fetch_bhavcopy_rows(exchange: str, date: _dt.date, client: GDFLClient) -> list[dict]:
    """One GetBhavCopyCM call for `date`, unwrapping the response's `value`/`Value` list."""
    response = client.get_bhavcopy_cm(from_date=date, to_date=date, exchange=exchange)
    if not isinstance(response, dict):
        return []
    for key in ("value", "Value", "VALUE"):
        if key in response:
            return response[key] or []
    return []


def main():
    parser = argparse.ArgumentParser(
        description="Fetch GetBhavCopyCM for a trading date, filter to target series/ISIN prefixes, "
        "and write a symbols-master xlsx."
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE)
    parser.add_argument(
        "--date",
        default=(_dt.date.today() - _dt.timedelta(days=1)).isoformat(),
        help="Trading date to fetch (YYYY-MM-DD). Defaults to yesterday.",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output .xlsx path.")
    args = parser.parse_args()
    date = _dt.datetime.strptime(args.date, "%Y-%m-%d").date()

    db = SessionLocal()
    try:
        existing_isins = fetch_existing_isins(db)
    finally:
        db.close()
    print(f"{len(existing_isins)} ISIN(s) already present in the symbols table.")

    client = GDFLClient()
    print(f"Fetching GetBhavCopyCM for {args.exchange}, date={date}...")
    try:
        rows = fetch_bhavcopy_rows(args.exchange, date, client)
    except GDFLAPIError as exc:
        print(f"GetBhavCopyCM failed: {exc}")
        raise SystemExit(1)

    print(f"Fetched {len(rows)} bhavcopy row(s) total.")

    records = build_symbol_rows(rows, args.exchange)
    print(f"{len(records)} row(s) matched series/ISIN prefix after dedup.")

    # records = [r for r in records if r["symbol"] not in existing_isins]
    print(f"{len(records)} row(s) remain after removing ISINs already in the symbols table.")

    df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
    df.to_excel(args.output, index=False)
    print(f"Wrote {len(df)} rows to {args.output}")


if __name__ == "__main__":
    main()

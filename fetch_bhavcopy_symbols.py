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
high_52_week, low_52_week. industry_id/sector_id aren't returned by
GetInstruments either, but are filled in separately below via
GetSectoralClassification; logo_url/listed_date/archive still aren't
populated by anything here.

After writing the xlsx, every fetched row is also upserted into the
`symbols` table, matching primarily by `isin` (see upsert_symbols()):
  - `isin` matches a row -> that row's `symbol` is corrected to the fetched
    symbol if different, and its `exchange` is corrected to the fetched
    exchange whenever it's currently "NSE" (an ISIN also seen on this
    exchange means the row was mislabeled NSE).
  - no isin match, but `symbol` matches a row -> update that row's isin
    (fallback for rows not yet carrying this isin).
  - `symbol` matches one row and `isin` matches a *different* row -> ambiguous
    data conflict, skipped (logged) rather than guessed at.
  - neither matches -> brand new symbol, inserted. Columns GetInstruments
    doesn't provide (logo_url, listed_date) are set to their type's empty
    value (`""`/`None`) rather than guessed at; high_52_week/low_52_week
    default to 0 when GetInstruments didn't return them either;
    industry_id/sector_id are left unset here (see below).

Every row touched above (matched or inserted) also has its `series` column
overwritten with the fetched `SERIES` value, and `final_status` set to
"ACCEPTED" -- GetInstruments' `onlyActive=true` means everything it returns
is, by definition, currently active/accepted. Conversely, EVERY `symbols`
row -- regardless of its own `exchange` (BSE, NSE, whatever) -- whose
symbol AND isin both fail to match anything in this BSE fetch (e.g.
delisted, dropped out of TARGET_SERIES, or simply never listed on BSE at
all) has its `final_status` cleared to `""`. This BSE fetch is this
script's only source of truth for "is this instrument currently
active/accepted" -- an NSE row isn't exempt just because it's NSE.
`symbols.final_status` is NOT NULL in the DB (default `'false'`), so `""`
is used as the "not currently accepted" sentinel rather than SQL NULL --
fetch_price_snapshots.py's default `--status ACCEPTED` filter already only
matches the exact string "ACCEPTED", so this distinction is enough to keep
stale symbols out of that downstream job.

Every GDFL call this script makes -- GetInstruments as well as
GetSectoralClassification below -- always uses exchange="BSE" (see
EXCHANGE); there's no --exchange flag. Every row touched by the upsert
above (inserted or matched, not skipped conflicts) is run through GDFL's
GetSectoralClassification API (see sector_industry.py), batched at its
25-instruments-per-request limit. Each
response row's `Sector`/`Industry` names are looked up in the `sectors`/
`industries` tables (case-insensitively); a name not seen before is
inserted there first, then the symbol row's sector_id/industry_id is
pointed at it. Both the upsert and the classification step commit (or roll
back, for --dry-run) together as one transaction.

Symbols the API doesn't return a classification for are left untouched
here -- see fetch_missing_sector_industry.py, which specifically targets
`symbols` rows with a null sector_id or industry_id.

Pass --dry-run to print every planned change (symbol/exchange/isin updates,
inserts, conflicts, sector/industry classifications) and roll back instead
of committing.

Usage:
    python fetch_bhavcopy_symbols.py
    python fetch_bhavcopy_symbols.py --output bhavcopy_symbols.xlsx
    python fetch_bhavcopy_symbols.py --dry-run
"""
from __future__ import annotations

import logging

import pandas as pd
import argparse

from dotenv import load_dotenv
from sqlalchemy import func

from db.models.symbol import Symbol
from db.session import SessionLocal
from gdfl import GDFLAPIError, GDFLClient
from logging_setup import configure_logging
from sector_industry import apply_sector_industry

load_dotenv()

logger = logging.getLogger(__name__)

# GetInstruments (and, per sector_industry.EXCHANGE, GetSectoralClassification
# too) is always called with this exchange -- not a CLI-configurable value.
EXCHANGE = "BSE"
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


def build_symbol_rows(instrument_rows: list[dict]) -> list[dict]:
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
                "exchange": EXCHANGE,
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


def upsert_symbols(db, records: list[dict], dry_run: bool = False) -> tuple[dict[str, int], list[Symbol]]:
    """
    Matches each fetched record (always exchange=EXCHANGE, i.e. "BSE" -- see
    module docstring) against the `symbols` table by ISIN first (the stable
    identifier): when a row's isin already exists, its `symbol` is corrected
    to the fetched symbol, and its `exchange` is corrected to EXCHANGE
    whenever it's currently "NSE" (an ISIN also seen on this exchange means
    the row was mislabeled NSE).

    Falls back to matching by symbol (updating that row's isin) when no
    isin match exists, inserts a brand new row when neither matches, and
    skips (with a logged warning) when symbol and isin each match a
    *different* existing row.

    Also sets `series` to the fetched value and `final_status` to
    "ACCEPTED" on every row it touches (new or matched) -- GetInstruments'
    `onlyActive=true` means everything it returns is, by definition, a
    currently-accepted/active instrument. Afterwards, every OTHER `symbols`
    row -- ANY exchange, not just BSE -- whose symbol AND isin both matched
    nothing in this fetch at all has its `final_status` cleared to "" --
    see module docstring.

    Does NOT commit/rollback -- the caller (run()) does that once, after
    also applying sector/industry classification, so both land in a single
    transaction. Returns (stats, touched_rows), where touched_rows is every
    Symbol row matched or inserted (excluding skipped conflicts) -- the set
    run() then runs through GetSectoralClassification.
    """
    by_symbol, by_isin = load_existing_symbols(db)
    stats = {
        "inserted": 0,
        "isin_updated": 0,
        "symbol_updated": 0,
        "exchange_updated": 0,
        "series_updated": 0,
        "final_status_updated": 0,
        "final_status_cleared": 0,
        "unchanged": 0,
        "conflicts": 0,
    }
    touched_rows: list[Symbol] = []
    prefix = "[DRY RUN] " if dry_run else ""

    for rec in records:
        symbol = (rec["symbol"] or "").strip().upper()
        isin = (rec["isin"] or "").strip().upper()
        rec_series = (rec["series"] or "").strip().upper()
        if not symbol:
            continue

        row_by_symbol = by_symbol.get(symbol)
        row_by_isin = by_isin.get(isin) if isin else None

        if row_by_symbol is not None and row_by_isin is not None and row_by_symbol is not row_by_isin:
            logger.warning(
                "%sCONFLICT: symbol=%r belongs to row id=%s but isin=%r belongs to a different row id=%s; skipping.",
                prefix, symbol, row_by_symbol.id, isin, row_by_isin.id,
            )
            stats["conflicts"] += 1
            continue

        if row_by_isin is not None:
            changes = []

            existing_symbol = (row_by_isin.symbol or "").strip().upper()
            if existing_symbol != symbol:
                changes.append(f"symbol {row_by_isin.symbol!r} -> {symbol!r}")
                if existing_symbol:
                    by_symbol.pop(existing_symbol, None)
                row_by_isin.symbol = symbol
                by_symbol[symbol] = row_by_isin
                stats["symbol_updated"] += 1

            existing_exchange = (row_by_isin.exchange or "").strip().upper()
            if existing_exchange == "NSE":
                changes.append(f"exchange {row_by_isin.exchange!r} -> {EXCHANGE!r}")
                row_by_isin.exchange = EXCHANGE
                stats["exchange_updated"] += 1

            if (row_by_isin.series or "").strip().upper() != rec_series:
                changes.append(f"series {row_by_isin.series!r} -> {rec_series!r}")
                row_by_isin.series = rec_series
                stats["series_updated"] += 1

            if row_by_isin.final_status != "ACCEPTED":
                changes.append(f"final_status {row_by_isin.final_status!r} -> 'ACCEPTED'")
                row_by_isin.final_status = "ACCEPTED"
                stats["final_status_updated"] += 1

            if changes:
                logger.info("%sISIN match %r (row id=%s): %s", prefix, isin, row_by_isin.id, ", ".join(changes))
            else:
                stats["unchanged"] += 1
            touched_rows.append(row_by_isin)
            continue

        if row_by_symbol is not None:
            changes = []

            existing_isin = (row_by_symbol.isin or "").strip().upper()
            if isin and existing_isin != isin:
                changes.append(f"isin {row_by_symbol.isin!r} -> {isin!r}")
                row_by_symbol.isin = isin
                by_isin[isin] = row_by_symbol
                stats["isin_updated"] += 1

            if (row_by_symbol.series or "").strip().upper() != rec_series:
                changes.append(f"series {row_by_symbol.series!r} -> {rec_series!r}")
                row_by_symbol.series = rec_series
                stats["series_updated"] += 1

            if row_by_symbol.final_status != "ACCEPTED":
                changes.append(f"final_status {row_by_symbol.final_status!r} -> 'ACCEPTED'")
                row_by_symbol.final_status = "ACCEPTED"
                stats["final_status_updated"] += 1

            if changes:
                logger.info("%ssymbol match %r (row id=%s): %s", prefix, symbol, row_by_symbol.id, ", ".join(changes))
            else:
                stats["unchanged"] += 1
            touched_rows.append(row_by_symbol)
            continue

        new_row = Symbol(
            symbol=symbol,
            name=rec["name"] or "",
            exchange=EXCHANGE,
            isin=isin or None,
            series=rec_series,
            high_52_week=rec["high_52_week"] if rec["high_52_week"] is not None else 0,
            low_52_week=rec["low_52_week"] if rec["low_52_week"] is not None else 0,
            logo_url="",
            listed_date=None,
            industry_id=None,
            sector_id=None,
            final_status="ACCEPTED",
            archive=False,
        )
        db.add(new_row)
        by_symbol[symbol] = new_row
        if isin:
            by_isin[isin] = new_row
        stats["inserted"] += 1
        touched_rows.append(new_row)
        logger.info("%snew symbol %r (isin=%r): would insert", prefix, symbol, isin)

    # Flush renames/inserts above to the DB BEFORE running the stale-symbol
    # query below. Without this, the query's `symbol NOT IN touched_symbols`
    # filter is evaluated by Postgres against each row's *pre-rename*
    # column value (autoflush is off), so a row this run just renamed (old
    # name not in touched_symbols, which only holds the new name) would
    # wrongly match as "not in this fetch" and have final_status cleared
    # right back out -- even though it demonstrably *was* just matched.
    db.flush()

    # Scoped to ALL symbols rows, not just exchange=EXCHANGE ones -- an NSE
    # row whose symbol/isin doesn't show up in this BSE fetch either gets
    # rejected too (it's not "NSE symbols are exempt", it's "GDFL's BSE
    # instrument master is the only source of truth this script has for
    # whether ANY row is still a live, accepted instrument").
    touched_symbols = {row.symbol.strip().upper() for row in touched_rows if row.symbol}
    stale_query = db.query(Symbol)
    if touched_symbols:
        stale_query = stale_query.filter(~func.upper(Symbol.symbol).in_(touched_symbols))
    for row in stale_query.all():
        if row.final_status != "":
            logger.warning(
                "%s%r (exchange=%r) not in BSE fetch: final_status %r -> ''",
                prefix, row.symbol, row.exchange, row.final_status,
            )
            row.final_status = ""
            stats["final_status_cleared"] += 1

    return stats, touched_rows


def fetch_instrument_rows(series_list: list[str], client: GDFLClient) -> list[dict]:
    """One GetInstruments call per series (server-side filtered), combined into a single list. Always exchange=EXCHANGE."""
    rows: list[dict] = []
    for series in series_list:
        response = client.get_instruments(
            exchange=EXCHANGE,
            series=series,
            only_active=True,
            show_dummy_isin=False,
            detailed_info=True,
        )
        series_rows = response.get("INSTRUMENTS", []) if isinstance(response, dict) else []
        logger.info("series=%s: %s instrument(s)", series, len(series_rows))
        rows.extend(series_rows)
    return rows


def run(
    write_xlsx: bool = False,
    output: str = DEFAULT_OUTPUT,
    db=None,
    dry_run: bool = False,
) -> dict:
    """
    Callable form of main()'s logic, for use by the scheduler (scheduler/jobs.py)
    as well as the CLI below. Returns the upsert stats dict plus row counts.
    Always operates on exchange=EXCHANGE ("BSE") -- there's no exchange arg.

    `db` lets a caller (e.g. the scheduler runner) pass in a session it already
    owns; when omitted, this opens and closes its own.
    """
    client = GDFLClient()
    logger.info("Fetching GetInstruments for %s, series=%s...", EXCHANGE, TARGET_SERIES)
    rows = fetch_instrument_rows(TARGET_SERIES, client)
    logger.info("Fetched %s instrument row(s) total.", len(rows))

    records = build_symbol_rows(rows)
    logger.info("%s row(s) matched ISIN prefix INE/IN after dedup.", len(records))

    if write_xlsx:
        df = pd.DataFrame(records, columns=OUTPUT_COLUMNS)
        df.to_excel(output, index=False)
        logger.info("Wrote %s rows to %s", len(df), output)

    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        stats, touched_rows = upsert_symbols(db, records, dry_run=dry_run)

        logger.info("Classifying %s symbol(s) via GetSectoralClassification...", len(touched_rows))
        classification_stats = apply_sector_industry(db, client, touched_rows, dry_run=dry_run)
        stats.update({f"classification_{key}": value for key, value in classification_stats.items()})

        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        if owns_db:
            db.close()

    stats["instrument_rows"] = len(rows)
    stats["matched_rows"] = len(records)
    prefix = "[DRY RUN] " if dry_run else ""
    logger.info(
        "%sDB upsert: %s inserted, %s isin updated, %s symbol updated, %s exchange updated, "
        "%s series updated, %s final_status set to ACCEPTED, %s final_status cleared (not in this fetch), "
        "%s unchanged, %s conflict(s) skipped.",
        prefix, stats["inserted"], stats["isin_updated"], stats["symbol_updated"], stats["exchange_updated"],
        stats["series_updated"], stats["final_status_updated"], stats["final_status_cleared"],
        stats["unchanged"], stats["conflicts"],
    )
    logger.info(
        "%sSector/industry: %s matched by API, %s sector updated, %s industry updated, %s unchanged.",
        prefix, classification_stats["api_matched"], classification_stats["sector_updated"],
        classification_stats["industry_updated"], classification_stats["unchanged"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Fetch GetInstruments (always exchange=BSE), filter to target series/ISIN prefixes, "
        "and write a symbols-master xlsx."
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output .xlsx path.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change (matches, symbol/exchange/isin updates, inserts) without committing to the DB.",
    )
    args = parser.parse_args()

    configure_logging("fetch_bhavcopy_symbols")
    try:
        run(write_xlsx=True, output=args.output, dry_run=args.dry_run)
    except GDFLAPIError:
        logger.exception("GetInstruments failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

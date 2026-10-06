"""
Backfills `symbols.sector_id`/`industry_id` for rows that don't have one yet,
via GDFL's GetSectoralClassification API.

fetch_bhavcopy_symbols.py already classifies every symbol it upserts on each
run, but this targets whatever's still left with a null sector_id or
industry_id -- rows inserted before that classification step existed, or
ones GDFL had no classification for on a previous run.

Classification calls always use exchange="BSE" (see sector_industry.EXCHANGE),
regardless of a row's own `exchange` column -- GDFL's sectoral classification
data is sourced from BSE. Rows are batched at the API's 25-instruments-per-
request limit -- see sector_industry.py, shared with fetch_bhavcopy_symbols.py.
A `Sector`/`Industry` name not already in the `sectors`/`industries` tables
is inserted there first, then the symbol row's sector_id/industry_id is
pointed at it.

Usage:
    python fetch_missing_sector_industry.py
    python fetch_missing_sector_industry.py --exchange BSE
    python fetch_missing_sector_industry.py --dry-run
"""
from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv
from sqlalchemy import or_

from db.models.symbol import Symbol
from db.session import SessionLocal
from gdfl import GDFLAPIError, GDFLClient
from logging_setup import configure_logging
from sector_industry import apply_sector_industry

load_dotenv()

logger = logging.getLogger(__name__)


def load_symbols_missing_classification(db, exchange: str | None = None) -> list[Symbol]:
    """Every `symbols` row with a null sector_id or industry_id, optionally scoped to one exchange."""
    query = db.query(Symbol).filter(or_(Symbol.sector_id.is_(None), Symbol.industry_id.is_(None)))
    if exchange:
        query = query.filter(Symbol.exchange == exchange)
    return query.all()


def run(exchange: str | None = None, db=None, dry_run: bool = False) -> dict:
    """
    `exchange` narrows which `symbols` rows are considered (matched against
    each row's own `exchange` column); when omitted, every `symbols` row
    missing a sector_id/industry_id is processed, regardless of exchange.
    `db` lets a caller pass in a session it already owns; when omitted,
    this opens and closes its own.
    """
    client = GDFLClient()
    owns_db = db is None
    if owns_db:
        db = SessionLocal()
    try:
        rows = load_symbols_missing_classification(db, exchange)
        scope = f" for exchange={exchange!r}" if exchange else ""
        logger.info("%s symbol(s) missing sector/industry%s.", len(rows), scope)

        stats = apply_sector_industry(db, client, rows, dry_run=dry_run)

        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        if owns_db:
            db.close()

    stats["symbols_checked"] = len(rows)
    prefix = "[DRY RUN] " if dry_run else ""
    logger.info(
        "%s%s matched by API, %s sector updated, %s industry updated, %s unchanged.",
        prefix, stats["api_matched"], stats["sector_updated"], stats["industry_updated"], stats["unchanged"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Backfill sector/industry for `symbols` rows missing either, via GetSectoralClassification."
    )
    parser.add_argument(
        "--exchange",
        default=None,
        help="Only process symbols rows with this exchange. Defaults to every exchange present among them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without committing to the DB.",
    )
    args = parser.parse_args()

    configure_logging("fetch_missing_sector_industry")
    try:
        run(exchange=args.exchange, dry_run=args.dry_run)
    except GDFLAPIError:
        logger.exception("GetSectoralClassification failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
